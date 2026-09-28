<script setup lang="ts">
// 镜像管理:列表 + 拉取（后台任务）+ 删除（输入名称确认）。
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import {
  formatBytes,
  isDockerUnavailable,
  listImages,
  pullImage,
  removeImage,
  shortId,
} from '@/api/docker'
import type { ImageSummary } from '@/api/docker'
import { problemMessage } from '@/api/problem'
import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'
import { formatDateTime } from '@/composables/format'
import { useJobsStore } from '@/stores/jobs'

const jobsStore = useJobsStore()

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['docker-images'],
  queryFn: listImages,
})

const images = computed(() => data.value?.images ?? [])
const errorMessage = computed(() => problemMessage(error.value, '获取镜像列表失败'))
const dockerUnavailable = computed(() => isDockerUnavailable(error.value))

/** 从错误对象里取 problem code（用于按错误码分支处理）。 */
function problemCode(err: unknown): string | undefined {
  return (err as ApiError | undefined)?.problem?.code
}

/** 镜像展示名:优先第一个标签，无标签时用短 id。 */
function imageName(image: ImageSummary): string {
  return image.tags[0] ?? shortId(image.id)
}

// —— 拉取镜像（202 后台任务） ——

const pullVisible = ref(false)
const pullSubmitting = ref(false)
const pullFormRef = ref<FormInstance>()
const pullForm = reactive({ repository: '', tag: 'latest' })

// 与后端 REPOSITORY_PATTERN / TAG_PATTERN 保持一致。
const pullRules: FormRules<typeof pullForm> = {
  repository: [
    { required: true, message: '请输入镜像仓库名', trigger: 'blur' },
    {
      pattern: /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}$/,
      message: '仓库名需以字母或数字开头，仅可含字母、数字与 _ . : / -',
      trigger: 'blur',
    },
  ],
  tag: [
    { required: true, message: '请输入标签', trigger: 'blur' },
    {
      pattern: /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/,
      message: '标签仅可含字母、数字与 _ . -',
      trigger: 'blur',
    },
  ],
}

function openPull(): void {
  pullForm.repository = ''
  pullForm.tag = 'latest'
  pullVisible.value = true
}

async function handlePull(): Promise<void> {
  if (!pullFormRef.value) {
    return
  }
  const valid = await pullFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  pullSubmitting.value = true
  try {
    const { job_id } = await pullImage({ repository: pullForm.repository, tag: pullForm.tag })
    ElMessage.success('拉取任务已提交，可在任务中心查看进度')
    pullVisible.value = false
    jobsStore.openDrawer(job_id)
  } catch (err) {
    ElMessage.error(problemMessage(err, '提交拉取任务失败'))
  } finally {
    pullSubmitting.value = false
  }
}

// —— 删除镜像（输入名称确认） ——

const removeVisible = ref(false)
const removeLoading = ref(false)
const removeTarget = ref<ImageSummary | null>(null)

function openRemove(row: ImageSummary): void {
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
    // 按镜像 id 删除，语义为删除整个镜像（而非仅取消某个标签）。
    await removeImage(target.id)
    ElMessage.success(`镜像「${imageName(target)}」已删除`)
    removeVisible.value = false
    await refetch()
  } catch (err) {
    if (problemCode(err) === 'conflict') {
      ElMessage.error('镜像仍被容器使用或存在多个标签，无法删除')
    } else {
      ElMessage.error(problemMessage(err, '删除镜像失败'))
    }
  } finally {
    removeLoading.value = false
  }
}
</script>

<template>
  <div class="images-page">
    <el-card shadow="never">
      <template #header>
        <div class="images-page__header">
          <span class="images-page__title">镜像</span>
          <div>
            <el-button link type="primary" :loading="isPending" @click="() => refetch()">
              刷新
            </el-button>
            <el-button type="primary" @click="openPull">拉取镜像</el-button>
          </div>
        </div>
      </template>

      <el-skeleton v-if="isPending" :rows="5" animated />
      <el-empty v-else-if="dockerUnavailable" description="Docker 守护进程不可用">
        <p class="images-page__hint">请确认 Docker 服务已在主机上启动，然后重试。</p>
        <el-button type="primary" @click="() => refetch()">重试</el-button>
      </el-empty>
      <el-alert
        v-else-if="isError"
        type="error"
        :title="errorMessage"
        :closable="false"
        show-icon
      />
      <el-table v-else :data="images">
        <template #empty>
          <el-empty description="暂无镜像" />
        </template>
        <el-table-column label="标签" min-width="220">
          <template #default="{ row }">
            <template v-if="row.tags.length">
              <el-tag
                v-for="tag in row.tags"
                :key="tag"
                size="small"
                effect="plain"
                class="images-page__tag"
              >
                {{ tag }}
              </el-tag>
            </template>
            <el-tag v-else size="small" type="info" effect="plain">无标签</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="ID" width="140">
          <template #default="{ row }">
            <code class="images-page__id">{{ shortId(row.id) }}</code>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="110">
          <template #default="{ row }">{{ formatBytes(row.size) }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created * 1000) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" @click="openRemove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 拉取镜像 -->
    <el-dialog v-model="pullVisible" title="拉取镜像" width="480px">
      <el-form
        ref="pullFormRef"
        :model="pullForm"
        :rules="pullRules"
        label-position="top"
        @submit.prevent="handlePull"
      >
        <el-form-item label="仓库（可含镜像源地址）" prop="repository">
          <el-input v-model="pullForm.repository" placeholder="如 nginx 或 ghcr.io/user/app" />
        </el-form-item>
        <el-form-item label="标签" prop="tag">
          <el-input v-model="pullForm.tag" placeholder="latest" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pullVisible = false">取消</el-button>
        <el-button type="primary" :loading="pullSubmitting" @click="handlePull">开始拉取</el-button>
      </template>
    </el-dialog>

    <!-- 删除确认（输入镜像标签或短 id） -->
    <ConfirmDangerDialog
      v-model="removeVisible"
      title="删除镜像"
      :resource-name="removeTarget ? imageName(removeTarget) : ''"
      message="删除镜像不可恢复；被容器使用中的镜像无法删除。"
      :loading="removeLoading"
      @confirm="handleRemove"
    />
  </div>
</template>

<style scoped>
.images-page__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.images-page__title {
  font-weight: 600;
}

.images-page__tag {
  margin: 2px 8px 2px 0;
}

.images-page__id {
  font-family: var(--el-font-family-mono, 'JetBrains Mono', Consolas, monospace);
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.images-page__hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
