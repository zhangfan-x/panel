<script setup lang="ts">
import { computed } from 'vue'
import { useQuery } from '@tanstack/vue-query'

import { getSystemInfo } from '@/api/system'
import { problemMessage } from '@/api/problem'
import { formatUptime } from '@/composables/format'

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['system-info'],
  queryFn: getSystemInfo,
})

const infoItems = computed(() => {
  const info = data.value
  if (!info) {
    return []
  }
  return [
    { label: '主机名', value: info.hostname },
    { label: '操作系统', value: info.os_release },
    { label: '内核', value: info.kernel },
    { label: '架构', value: info.arch },
    { label: 'CPU 核数', value: String(info.cpu_count) },
    { label: '运行时长', value: formatUptime(info.uptime_s) },
  ]
})

const errorMessage = computed(() => problemMessage(error.value, '获取系统信息失败'))

const placeholderCards = [
  { title: 'CPU / 负载', hint: 'CPU 使用率与负载曲线' },
  { title: '内存 / Swap', hint: '内存与交换分区使用' },
  { title: '磁盘 / 挂载点', hint: '磁盘容量与 inode' },
  { title: '网络吞吐', hint: '实时上传 / 下载速率' },
]
</script>

<template>
  <div class="dashboard">
    <el-alert
      class="dashboard__notice"
      type="info"
      :closable="false"
      show-icon
      title="监控图表将在 M2 提供"
      description="当前仅展示主机基础信息，实时指标、图表与安全事件将在后续里程碑接入。"
    />

    <el-card shadow="never" class="dashboard__card">
      <template #header>
        <div class="dashboard__card-header">
          <span>主机信息</span>
          <el-button link type="primary" :loading="isPending" @click="() => refetch()"
            >刷新</el-button
          >
        </div>
      </template>

      <el-skeleton v-if="isPending" :rows="5" animated />

      <el-alert
        v-else-if="isError"
        type="error"
        :title="errorMessage"
        :closable="false"
        show-icon
      />

      <el-descriptions v-else :column="2" border>
        <el-descriptions-item v-for="item in infoItems" :key="item.label" :label="item.label">
          {{ item.value }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-row :gutter="16" class="dashboard__grid">
      <el-col v-for="card in placeholderCards" :key="card.title" :xs="24" :sm="12" :lg="6">
        <el-card shadow="never" class="dashboard__metric">
          <div class="dashboard__metric-title">{{ card.title }}</div>
          <el-empty :description="card.hint" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.dashboard__notice {
  margin-bottom: 16px;
}

.dashboard__card {
  margin-bottom: 16px;
}

.dashboard__card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.dashboard__metric-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 8px;
}
</style>
