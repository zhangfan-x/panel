<script setup lang="ts">
// 任务中心：任务列表（状态过滤 + 分页 + 实时更新）、日志抽屉、取消/重试。
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { storeToRefs } from 'pinia'

import { JOB_STATUS_META, jobLogUrl } from '@/api/jobs'
import type { JobStatus, JobStatusMeta, JobSummary } from '@/api/jobs'
import { problemMessage } from '@/api/problem'
import LogViewer from '@/components/LogViewer.vue'
import { formatDateTime } from '@/composables/format'
import { useJobsStore } from '@/stores/jobs'

/** 状态过滤选项的展示顺序。 */
const STATUS_ORDER: JobStatus[] = [
  'queued',
  'running',
  'succeeded',
  'failed',
  'cancel_requested',
  'cancelled',
]

const jobsStore = useJobsStore()
const { jobs, total, loading, statusFilter, limit, offset } = storeToRefs(jobsStore)

const logVisible = ref(false)
const logJob = ref<JobSummary | null>(null)

/** 状态过滤（'' 表示全部）；变更后回到第一页并刷新。 */
const filterValue = computed<'' | JobStatus>({
  get: () => statusFilter.value ?? '',
  set: (value) => {
    statusFilter.value = value === '' ? null : value
    offset.value = 0
    void load()
  },
})

/** 当前页码（由 offset/limit 推导）；翻页时刷新。 */
const currentPage = computed<number>({
  get: () => Math.floor(offset.value / limit.value) + 1,
  set: (page) => {
    offset.value = (page - 1) * limit.value
    void load()
  },
})

const logTitle = computed(() => (logJob.value ? `任务日志：${logJob.value.title}` : '任务日志'))

function statusMeta(status: JobStatus): JobStatusMeta {
  return JOB_STATUS_META[status]
}

function progressValue(job: JobSummary): number {
  return Math.min(100, Math.max(0, job.progress ?? 0))
}

function createdText(job: JobSummary): string {
  return job.created_at ? formatDateTime(job.created_at) : '—'
}

/** 任务耗时（未开始显示「—」，运行中按当前时间估算）。 */
function durationText(job: JobSummary): string {
  if (!job.started_at) {
    return '—'
  }
  const start = Date.parse(job.started_at)
  const end = job.finished_at ? Date.parse(job.finished_at) : Date.now()
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    return '—'
  }
  const seconds = Math.round((end - start) / 1000)
  if (seconds < 60) {
    return `${seconds} 秒`
  }
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

function showCancel(job: JobSummary): boolean {
  return job.status === 'queued' || job.status === 'running' || job.status === 'cancel_requested'
}

function showRetry(job: JobSummary): boolean {
  return job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled'
}

async function load(): Promise<void> {
  try {
    await jobsStore.refresh()
  } catch (error) {
    ElMessage.error(problemMessage(error, '获取任务列表失败'))
  }
}

function handleRefresh(): void {
  void load()
}

function openLog(job: JobSummary): void {
  logJob.value = job
  logVisible.value = true
}

async function handleCancel(job: JobSummary): Promise<void> {
  try {
    await jobsStore.cancel(job.id)
    ElMessage.success('已请求取消该任务')
  } catch (error) {
    ElMessage.error(problemMessage(error, '取消任务失败'))
  }
}

async function handleRetry(job: JobSummary): Promise<void> {
  try {
    const newId = await jobsStore.retry(job.id)
    ElMessage.success('已重新提交任务')
    const fresh = jobs.value.find((item) => item.id === newId)
    if (fresh) {
      openLog(fresh)
    }
  } catch (error) {
    ElMessage.error(problemMessage(error, '重试任务失败'))
  }
}

onMounted(handleRefresh)
</script>

<template>
  <div class="tasks-page">
    <el-card shadow="never">
      <template #header>
        <div class="tasks-page__header">
          <span class="tasks-page__heading">任务中心</span>
          <el-button link type="primary" :loading="loading" @click="handleRefresh">刷新</el-button>
        </div>
      </template>

      <div class="tasks-page__filters">
        <el-radio-group v-model="filterValue" size="small">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button v-for="status in STATUS_ORDER" :key="status" :value="status">
            {{ statusMeta(status).text }}
          </el-radio-button>
        </el-radio-group>
      </div>

      <el-table v-loading="loading" :data="jobs" empty-text="暂无任务" class="tasks-page__table">
        <el-table-column label="任务" min-width="240">
          <template #default="{ row }">
            <div class="tasks-page__title" :title="row.title">{{ row.title }}</div>
            <div class="tasks-page__type">{{ row.type }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="200">
          <template #default="{ row }">
            <el-tag size="small" :type="statusMeta(row.status).tagType">
              {{ statusMeta(row.status).text }}
            </el-tag>
            <el-progress
              v-if="row.status === 'running' && row.progress !== null"
              :percentage="progressValue(row)"
              :stroke-width="6"
              class="tasks-page__progress"
            />
            <div v-if="row.stage" class="tasks-page__stage">{{ row.stage }}</div>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ createdText(row) }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="110">
          <template #default="{ row }">{{ durationText(row) }}</template>
        </el-table-column>
        <el-table-column label="失败原因" min-width="180">
          <template #default="{ row }">
            <span v-if="row.error_message" class="tasks-page__error" :title="row.error_message">
              {{ row.error_message }}
            </span>
            <span v-else class="tasks-page__none">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openLog(row)">日志</el-button>
            <el-button
              v-if="showCancel(row)"
              link
              type="warning"
              size="small"
              :disabled="row.status === 'cancel_requested'"
              @click="handleCancel(row)"
            >
              取消
            </el-button>
            <el-button
              v-if="showRetry(row)"
              link
              type="primary"
              size="small"
              @click="handleRetry(row)"
            >
              重试
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="tasks-page__footer">
        <el-pagination
          v-model:current-page="currentPage"
          layout="total, prev, pager, next"
          :total="total"
          :page-size="limit"
        />
      </div>
    </el-card>

    <el-drawer v-model="logVisible" :title="logTitle" size="55%" destroy-on-close>
      <LogViewer v-if="logJob" :source="jobLogUrl(logJob.id)" height="calc(100vh - 180px)" />
    </el-drawer>
  </div>
</template>

<style scoped>
.tasks-page__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.tasks-page__heading {
  font-size: 16px;
  font-weight: 600;
}

.tasks-page__filters {
  margin-bottom: 16px;
}

.tasks-page__title {
  font-weight: 500;
  color: var(--el-text-color-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tasks-page__type {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tasks-page__progress {
  margin-top: 6px;
}

.tasks-page__stage {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tasks-page__error {
  display: inline-block;
  max-width: 100%;
  font-size: 12px;
  color: var(--el-color-danger);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tasks-page__none {
  color: var(--el-text-color-placeholder);
}

.tasks-page__footer {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
