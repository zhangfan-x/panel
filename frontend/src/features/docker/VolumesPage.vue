<script setup lang="ts">
// 卷管理:列表 + 创建命名卷 + 删除（输入名称确认）。
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import { createVolume, isDockerUnavailable, listVolumes, removeVolume } from '@/api/docker'
import type { VolumeSummary } from '@/api/docker'
import { problemMessage } from '@/api/problem'
import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'
import { formatDateTime } from '@/composables/format'

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['docker-volumes'],
  queryFn: listVolumes,
})

const volumes = computed(() => data.value?.volumes ?? [])
const errorMessage = computed(() => problemMessage(error.value, '获取卷列表失败'))
const dockerUnavailable = computed(() => isDockerUnavailable(error.value))

/** 从错误对象里取 problem code（用于按错误码分支处理）。 */
function problemCode(err: unknown): string | undefined {
  return (err as ApiError | undefined)?.problem?.code
}

function createdText(row: VolumeSummary): string {
  return row.created ? formatDateTime(row.created) : '—'
}

// —— 创建卷 ——

const createVisible = ref(false)
const createSubmitting = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = reactive({ name: '' })

const createRules: FormRules<typeof createForm> = {
  name: [
    { required: true, message: '请输入卷名称', trigger: 'blur' },
    {
      pattern: /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/,
      message: '名称需以字母或数字开头，仅可含字母、数字与 _ . -',
      trigger: 'blur',
    },
  ],
}

function openCreate(): void {
  createForm.name = ''
  createVisible.value = true
}

async function handleCreate(): Promise<void> {
  if (!createFormRef.value) {
    return
  }
  const valid = await createFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  createSubmitting.value = true
  try {
    await createVolume(createForm.name)
    ElMessage.success(`卷「${createForm.name}」已创建`)
    createVisible.value = false
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '创建卷失败'))
  } finally {
    createSubmitting.value = false
  }
}

// —— 删除卷（输入名称确认） ——

const removeVisible = ref(false)
const removeLoading = ref(false)
const removeTarget = ref<VolumeSummary | null>(null)

function openRemove(row: VolumeSummary): void {
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
    await removeVolume(target.name)
    ElMessage.success(`卷「${target.name}」已删除`)
    removeVisible.value = false
    await refetch()
  } catch (err) {
    if (problemCode(err) === 'conflict') {
      ElMessage.error('卷仍被容器使用，无法删除')
    } else {
      ElMessage.error(problemMessage(err, '删除卷失败'))
    }
  } finally {
    removeLoading.value = false
  }
}
</script>

<template>
  <div class="volumes-page">
    <el-card shadow="never">
      <template #header>
        <div class="volumes-page__header">
          <span class="volumes-page__title">卷</span>
          <div>
            <el-button link type="primary" :loading="isPending" @click="() => refetch()">
              刷新
            </el-button>
            <el-button type="primary" @click="openCreate">创建卷</el-button>
          </div>
        </div>
      </template>

      <el-skeleton v-if="isPending" :rows="5" animated />
      <el-empty v-else-if="dockerUnavailable" description="Docker 守护进程不可用">
        <p class="volumes-page__hint">请确认 Docker 服务已在主机上启动，然后重试。</p>
        <el-button type="primary" @click="() => refetch()">重试</el-button>
      </el-empty>
      <el-alert
        v-else-if="isError"
        type="error"
        :title="errorMessage"
        :closable="false"
        show-icon
      />
      <el-table v-else :data="volumes">
        <template #empty>
          <el-empty description="暂无卷" />
        </template>
        <el-table-column label="名称" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="volumes-page__name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="driver" label="驱动" width="100" />
        <el-table-column prop="mountpoint" label="挂载点" min-width="240" show-overflow-tooltip />
        <el-table-column prop="scope" label="范围" width="100" />
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ createdText(row) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" @click="openRemove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 创建卷 -->
    <el-dialog v-model="createVisible" title="创建卷" width="480px">
      <el-form
        ref="createFormRef"
        :model="createForm"
        :rules="createRules"
        label-position="top"
        @submit.prevent="handleCreate"
      >
        <el-form-item label="名称" prop="name">
          <el-input v-model="createForm.name" placeholder="如 app-data" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createSubmitting" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 删除确认（输入卷名） -->
    <ConfirmDangerDialog
      v-model="removeVisible"
      title="删除卷"
      :resource-name="removeTarget?.name ?? ''"
      message="删除后卷中的数据不可恢复；仍被容器使用的卷无法删除。"
      :loading="removeLoading"
      @confirm="handleRemove"
    />
  </div>
</template>

<style scoped>
.volumes-page__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.volumes-page__title {
  font-weight: 600;
}

.volumes-page__name {
  font-weight: 500;
}

.volumes-page__hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
