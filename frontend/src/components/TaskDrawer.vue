<script setup lang="ts">
// 任务抽屉：全局挂载的任务中心速览（列表 + 状态标签 + 进度 + 取消/重试 + 展开日志流）。
import { watch } from 'vue'
import { ElMessage } from 'element-plus'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'

import { JOB_STATUS_META, jobLogUrl } from '@/api/jobs'
import type { JobStatus, JobStatusMeta, JobSummary } from '@/api/jobs'
import { problemMessage } from '@/api/problem'
import LogViewer from '@/components/LogViewer.vue'
import { formatDateTime } from '@/composables/format'
import { useJobsStore } from '@/stores/jobs'

const jobsStore = useJobsStore()
const { drawerOpen, jobs, loading, activeLogJobId } = storeToRefs(jobsStore)
const router = useRouter()

// 抽屉每次打开时刷新一次列表（后续增量更新走全局 SSE）。
watch(drawerOpen, (open) => {
  if (open) {
    void handleRefresh()
  }
})

function statusMeta(status: JobStatus): JobStatusMeta {
  return JOB_STATUS_META[status]
}

function showCancel(job: JobSummary): boolean {
  return job.status === 'queued' || job.status === 'running' || job.status === 'cancel_requested'
}

function showRetry(job: JobSummary): boolean {
  return job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled'
}

function progressValue(job: JobSummary): number {
  return Math.min(100, Math.max(0, job.progress ?? 0))
}

async function handleRefresh(): Promise<void> {
  try {
    await jobsStore.refresh()
  } catch (error) {
    ElMessage.error(problemMessage(error, '获取任务列表失败'))
  }
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
    jobsStore.showLog(newId)
  } catch (error) {
    ElMessage.error(problemMessage(error, '重试任务失败'))
  }
}

function toggleLog(job: JobSummary): void {
  jobsStore.showLog(activeLogJobId.value === job.id ? null : job.id)
}

async function goTasksPage(): Promise<void> {
  jobsStore.closeDrawer()
  await router.push({ name: 'tasks' })
}
</script>

<template>
  <el-drawer
    v-model="drawerOpen"
    title="任务中心"
    size="520px"
    :append-to-body="true"
    @close="jobsStore.closeDrawer()"
  >
    <div v-loading="loading" class="task-drawer">
      <div class="task-drawer__toolbar">
        <el-button size="small" :loading="loading" @click="handleRefresh">刷新</el-button>
        <el-button size="small" link type="primary" @click="goTasksPage">前往任务中心</el-button>
      </div>
      <el-empty v-if="jobs.length === 0" description="暂无任务" :image-size="80" />
      <div v-for="job in jobs" :key="job.id" class="task-drawer__item">
        <div class="task-drawer__row">
          <span class="task-drawer__title" :title="job.title">{{ job.title }}</span>
          <el-tag size="small" :type="statusMeta(job.status).tagType">
            {{ statusMeta(job.status).text }}
          </el-tag>
        </div>
        <el-progress
          v-if="job.status === 'running' && job.progress !== null"
          :percentage="progressValue(job)"
          :stroke-width="6"
          class="task-drawer__progress"
        />
        <div class="task-drawer__meta">
          <span>{{ job.created_at ? formatDateTime(job.created_at) : '—' }}</span>
          <span v-if="job.stage">阶段：{{ job.stage }}</span>
        </div>
        <div v-if="job.error_message" class="task-drawer__error" :title="job.error_message">
          {{ job.error_message }}
        </div>
        <div class="task-drawer__actions">
          <el-button size="small" link type="primary" @click="toggleLog(job)">
            {{ activeLogJobId === job.id ? '收起日志' : '日志' }}
          </el-button>
          <el-button
            v-if="showCancel(job)"
            size="small"
            link
            type="warning"
            :disabled="job.status === 'cancel_requested'"
            @click="handleCancel(job)"
          >
            取消
          </el-button>
          <el-button
            v-if="showRetry(job)"
            size="small"
            link
            type="primary"
            @click="handleRetry(job)"
          >
            重试
          </el-button>
        </div>
        <LogViewer
          v-if="activeLogJobId === job.id"
          :source="jobLogUrl(job.id)"
          height="220px"
          class="task-drawer__log"
        />
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.task-drawer__toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.task-drawer__item {
  padding: 12px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.task-drawer__row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.task-drawer__title {
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-drawer__progress {
  margin-top: 8px;
}

.task-drawer__meta {
  margin-top: 6px;
  display: flex;
  gap: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.task-drawer__error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-color-danger);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-drawer__actions {
  margin-top: 6px;
  display: flex;
  align-items: center;
}

.task-drawer__log {
  margin-top: 8px;
}
</style>
