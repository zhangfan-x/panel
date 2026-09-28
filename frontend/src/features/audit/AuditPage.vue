<script setup lang="ts">
// 审计日志：可过滤（动作/资源/结果/时间范围）+ 分页的只读事件列表，附详情抽屉。
import { computed, reactive, ref } from 'vue'
import { keepPreviousData, useQuery } from '@tanstack/vue-query'

import { listAuditEvents } from '@/api/audit'
import type { AuditEvent, AuditResult, ListAuditParams } from '@/api/audit'
import { problemMessage } from '@/api/problem'
import { formatDateTime } from '@/composables/format'

/** 每页条数（后端 limit 上限 200）。 */
const PAGE_SIZE = 50

/** el-tag 的 type 取值。 */
type TagType = 'success' | 'danger' | 'info'

/** 过滤表单（点击「查询」后才生效，与已生效参数分离）。 */
const filterForm = reactive({
  action: '',
  resource: '',
  result: '' as '' | AuditResult,
  range: null as [Date, Date] | null,
})

/** 已生效的查询参数（作为 queryKey 的一部分，变化即自动重新拉取）。 */
const appliedParams = ref<ListAuditParams>({ limit: PAGE_SIZE, offset: 0 })

const { data, isError, error, isFetching, refetch } = useQuery({
  queryKey: ['audit-events', appliedParams],
  queryFn: () => listAuditEvents(appliedParams.value),
  // 翻页/改过滤时保留旧数据，避免表格闪烁成骨架屏。
  placeholderData: keepPreviousData,
})

const events = computed<AuditEvent[]>(() => data.value?.events ?? [])
const total = computed(() => data.value?.total ?? 0)
const errorMessage = computed(() => problemMessage(error.value, '获取审计日志失败'))

/** 当前页码（由 offset 推导，写入时更新 offset 触发刷新）。 */
const currentPage = computed<number>({
  get: () => Math.floor((appliedParams.value.offset ?? 0) / PAGE_SIZE) + 1,
  set: (page) => {
    appliedParams.value = { ...appliedParams.value, offset: (page - 1) * PAGE_SIZE }
  },
})

const detailVisible = ref(false)
const selected = ref<AuditEvent | null>(null)

/** 由过滤表单构造查询参数（空值不发送；时间转 ISO 字符串）。 */
function buildParams(offset: number): ListAuditParams {
  const params: ListAuditParams = { limit: PAGE_SIZE, offset }
  const action = filterForm.action.trim()
  const resource = filterForm.resource.trim()
  if (action) {
    params.action = action
  }
  if (resource) {
    params.resource = resource
  }
  if (filterForm.result) {
    params.result = filterForm.result
  }
  const [since, until] = filterForm.range ?? []
  if (since) {
    params.since = since.toISOString()
  }
  if (until) {
    params.until = until.toISOString()
  }
  return params
}

function handleSearch(): void {
  appliedParams.value = buildParams(0)
}

function handleReset(): void {
  filterForm.action = ''
  filterForm.resource = ''
  filterForm.result = ''
  filterForm.range = null
  handleSearch()
}

function handleRefresh(): void {
  void refetch()
}

function openDetail(row: AuditEvent): void {
  selected.value = row
  detailVisible.value = true
}

/** 结果列展示元数据（未知取值原样展示）。 */
function resultMeta(result: string): { text: string; tagType: TagType } {
  if (result === 'ok') {
    return { text: '成功', tagType: 'success' }
  }
  if (result === 'denied') {
    return { text: '拒绝', tagType: 'danger' }
  }
  return { text: result, tagType: 'info' }
}

/** 空串/空值统一显示为「—」。 */
function textOrDash(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  return String(value)
}
</script>

<template>
  <div class="audit-page">
    <el-card shadow="never">
      <template #header>
        <div class="audit-page__header">
          <span class="audit-page__heading">审计日志</span>
          <el-button link type="primary" :loading="isFetching" @click="handleRefresh">
            刷新
          </el-button>
        </div>
      </template>

      <el-form inline class="audit-page__filters" @submit.prevent="handleSearch">
        <el-form-item label="动作">
          <el-input
            v-model="filterForm.action"
            clearable
            placeholder="如 auth.login（精确匹配）"
            class="audit-page__filter-action"
            @clear="handleSearch"
            @keyup.enter="handleSearch"
          />
        </el-form-item>
        <el-form-item label="资源">
          <el-input
            v-model="filterForm.resource"
            clearable
            placeholder="如 auth（精确匹配）"
            class="audit-page__filter-resource"
            @clear="handleSearch"
            @keyup.enter="handleSearch"
          />
        </el-form-item>
        <el-form-item label="结果">
          <el-select v-model="filterForm.result" class="audit-page__filter-result">
            <el-option label="全部" value="" />
            <el-option label="成功" value="ok" />
            <el-option label="拒绝" value="denied" />
          </el-select>
        </el-form-item>
        <el-form-item label="时间范围">
          <el-date-picker
            v-model="filterForm.range"
            type="datetimerange"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">查询</el-button>
          <el-button @click="handleReset">重置</el-button>
        </el-form-item>
      </el-form>

      <el-alert
        v-if="isError"
        type="error"
        :title="errorMessage"
        :closable="false"
        show-icon
        class="audit-page__alert"
      />

      <el-table
        v-loading="isFetching"
        :data="events"
        empty-text="暂无审计记录"
        class="audit-page__table"
      >
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.ts) }}</template>
        </el-table-column>
        <el-table-column label="动作" min-width="170">
          <template #default="{ row }">
            <span class="audit-page__mono">{{ row.action }}</span>
          </template>
        </el-table-column>
        <el-table-column label="资源" width="110" prop="resource" />
        <el-table-column label="目标" min-width="160">
          <template #default="{ row }">
            <span class="audit-page__ellipsis" :title="row.target">
              {{ textOrDash(row.target) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="resultMeta(row.result).tagType">
              {{ resultMeta(row.result).text }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="错误码" min-width="140">
          <template #default="{ row }">
            <span class="audit-page__mono">{{ textOrDash(row.error_code) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="来源 IP" width="130">
          <template #default="{ row }">{{ textOrDash(row.source_ip) }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="90">
          <template #default="{ row }">{{ row.duration_ms }} ms</template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="audit-page__footer">
        <el-pagination
          v-model:current-page="currentPage"
          layout="total, prev, pager, next"
          :total="total"
          :page-size="PAGE_SIZE"
        />
      </div>
    </el-card>

    <el-drawer v-model="detailVisible" title="审计事件详情" size="480px">
      <el-descriptions v-if="selected" :column="1" border>
        <el-descriptions-item label="事件 ID">
          <span class="audit-page__mono">{{ selected.event_id }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="时间">{{ formatDateTime(selected.ts) }}</el-descriptions-item>
        <el-descriptions-item label="请求 ID">
          <span class="audit-page__mono">{{ textOrDash(selected.request_id) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="操作者用户 ID">
          {{ textOrDash(selected.actor_user_id) }}
        </el-descriptions-item>
        <el-descriptions-item label="来源 IP">
          {{ textOrDash(selected.source_ip) }}
        </el-descriptions-item>
        <el-descriptions-item label="User-Agent">
          <span class="audit-page__break">{{ textOrDash(selected.user_agent) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="资源">{{ selected.resource }}</el-descriptions-item>
        <el-descriptions-item label="动作">
          <span class="audit-page__mono">{{ selected.action }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="目标">
          <span class="audit-page__break">{{ textOrDash(selected.target) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="结果">
          <el-tag size="small" :type="resultMeta(selected.result).tagType">
            {{ resultMeta(selected.result).text }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="错误码">
          <span class="audit-page__mono">{{ textOrDash(selected.error_code) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="耗时">{{ selected.duration_ms }} ms</el-descriptions-item>
      </el-descriptions>
    </el-drawer>
  </div>
</template>

<style scoped>
.audit-page__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.audit-page__heading {
  font-size: 16px;
  font-weight: 600;
}

.audit-page__filters {
  margin-bottom: 4px;
}

.audit-page__filter-action {
  width: 220px;
}

.audit-page__filter-resource {
  width: 160px;
}

.audit-page__filter-result {
  width: 110px;
}

.audit-page__alert {
  margin-bottom: 16px;
}

.audit-page__mono {
  font-family: var(--el-font-family-mono, monospace);
  font-size: 12px;
}

.audit-page__ellipsis {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}

.audit-page__break {
  word-break: break-all;
}

.audit-page__footer {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
