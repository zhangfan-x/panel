<script setup lang="ts">
// 网络管理:列表 + 创建自定义网络 + 删除（输入名称确认；内置网络不可删）。
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import {
  createNetwork,
  isDockerUnavailable,
  listNetworks,
  removeNetwork,
  shortId,
} from '@/api/docker'
import type { NetworkDriver, NetworkSummary } from '@/api/docker'
import { problemMessage } from '@/api/problem'
import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'
import { formatDateTime } from '@/composables/format'

/** 引擎内置网络（删除必然被拒绝，不展示删除入口）。 */
const BUILTIN_NETWORKS = new Set(['bridge', 'host', 'none'])

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['docker-networks'],
  queryFn: listNetworks,
})

const networks = computed(() => data.value?.networks ?? [])
const errorMessage = computed(() => problemMessage(error.value, '获取网络列表失败'))
const dockerUnavailable = computed(() => isDockerUnavailable(error.value))

/** 从错误对象里取 problem code（用于按错误码分支处理）。 */
function problemCode(err: unknown): string | undefined {
  return (err as ApiError | undefined)?.problem?.code
}

function createdText(row: NetworkSummary): string {
  return row.created ? formatDateTime(row.created) : '—'
}

// —— 创建网络 ——

const DRIVERS: NetworkDriver[] = ['bridge', 'overlay', 'macvlan', 'ipvlan']

const createVisible = ref(false)
const createSubmitting = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = reactive({ name: '', driver: 'bridge' as NetworkDriver, internal: false })

const createRules: FormRules<typeof createForm> = {
  name: [
    { required: true, message: '请输入网络名称', trigger: 'blur' },
    {
      pattern: /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/,
      message: '名称需以字母或数字开头，仅可含字母、数字与 _ . -',
      trigger: 'blur',
    },
  ],
}

function openCreate(): void {
  createForm.name = ''
  createForm.driver = 'bridge'
  createForm.internal = false
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
    await createNetwork({
      name: createForm.name,
      driver: createForm.driver,
      internal: createForm.internal,
    })
    ElMessage.success(`网络「${createForm.name}」已创建`)
    createVisible.value = false
    await refetch()
  } catch (err) {
    if (problemCode(err) === 'conflict') {
      ElMessage.error('同名网络已存在')
    } else {
      ElMessage.error(problemMessage(err, '创建网络失败'))
    }
  } finally {
    createSubmitting.value = false
  }
}

// —— 删除网络（输入名称确认） ——

const removeVisible = ref(false)
const removeLoading = ref(false)
const removeTarget = ref<NetworkSummary | null>(null)

function openRemove(row: NetworkSummary): void {
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
    await removeNetwork(target.id)
    ElMessage.success(`网络「${target.name}」已删除`)
    removeVisible.value = false
    await refetch()
  } catch (err) {
    if (problemCode(err) === 'conflict') {
      ElMessage.error('仍有容器连接到该网络，无法删除')
    } else {
      ElMessage.error(problemMessage(err, '删除网络失败'))
    }
  } finally {
    removeLoading.value = false
  }
}
</script>

<template>
  <div class="networks-page">
    <el-card shadow="never">
      <template #header>
        <div class="networks-page__header">
          <span class="networks-page__title">网络</span>
          <div>
            <el-button link type="primary" :loading="isPending" @click="() => refetch()">
              刷新
            </el-button>
            <el-button type="primary" @click="openCreate">创建网络</el-button>
          </div>
        </div>
      </template>

      <el-skeleton v-if="isPending" :rows="5" animated />
      <el-empty v-else-if="dockerUnavailable" description="Docker 守护进程不可用">
        <p class="networks-page__hint">请确认 Docker 服务已在主机上启动，然后重试。</p>
        <el-button type="primary" @click="() => refetch()">重试</el-button>
      </el-empty>
      <el-alert
        v-else-if="isError"
        type="error"
        :title="errorMessage"
        :closable="false"
        show-icon
      />
      <el-table v-else :data="networks">
        <template #empty>
          <el-empty description="暂无网络" />
        </template>
        <el-table-column label="名称" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="networks-page__name">{{ row.name }}</span>
            <el-tag v-if="BUILTIN_NETWORKS.has(row.name)" size="small" type="info" effect="plain">
              内置
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="ID" width="140">
          <template #default="{ row }">
            <code class="networks-page__id">{{ shortId(row.id) }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="driver" label="驱动" width="110" />
        <el-table-column prop="scope" label="范围" width="100" />
        <el-table-column label="内部网络" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.internal" size="small" type="warning" effect="plain">是</el-tag>
            <span v-else class="networks-page__muted">否</span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ createdText(row) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="!BUILTIN_NETWORKS.has(row.name)"
              link
              type="danger"
              @click="openRemove(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 创建网络 -->
    <el-dialog v-model="createVisible" title="创建网络" width="480px">
      <el-form
        ref="createFormRef"
        :model="createForm"
        :rules="createRules"
        label-position="top"
        @submit.prevent="handleCreate"
      >
        <el-form-item label="名称" prop="name">
          <el-input v-model="createForm.name" placeholder="如 app-net" />
        </el-form-item>
        <el-form-item label="驱动" prop="driver">
          <el-select v-model="createForm.driver" class="networks-page__select">
            <el-option v-for="driver in DRIVERS" :key="driver" :label="driver" :value="driver" />
          </el-select>
        </el-form-item>
        <el-form-item label="内部网络（容器无法访问外部）" prop="internal">
          <el-switch v-model="createForm.internal" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createSubmitting" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 删除确认（输入网络名） -->
    <ConfirmDangerDialog
      v-model="removeVisible"
      title="删除网络"
      :resource-name="removeTarget?.name ?? ''"
      message="删除前请确认没有容器仍连接到该网络。"
      :loading="removeLoading"
      @confirm="handleRemove"
    />
  </div>
</template>

<style scoped>
.networks-page__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.networks-page__title {
  font-weight: 600;
}

.networks-page__name {
  margin-right: 8px;
  font-weight: 500;
}

.networks-page__id {
  font-family: var(--el-font-family-mono, 'JetBrains Mono', Consolas, monospace);
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.networks-page__muted {
  color: var(--el-text-color-secondary);
}

.networks-page__select {
  width: 100%;
}

.networks-page__hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
