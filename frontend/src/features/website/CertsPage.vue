<script setup lang="ts">
// 证书管理：列出 cert_dir 下的证书（解析主题/到期），上传证书 + 私钥、生成自签、删除。
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useQuery } from '@tanstack/vue-query'

import { deleteCert, generateSelfSigned, listCerts, uploadCert } from '@/api/website'
import type { CertSummary } from '@/api/website'
import { problemMessage } from '@/api/problem'
import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'
import { formatDateTime } from '@/composables/format'

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$/
const DOMAIN_RE = /^(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]{0,253})?$/

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['website-certs'],
  queryFn: listCerts,
})

const errorMessage = computed(() => problemMessage(error.value, '获取证书列表失败'))
const certs = computed(() => data.value?.certs ?? [])

/** 计算到期展示文案与标签色（已过期/临近/正常）。 */
function expiryInfo(cert: CertSummary): {
  text: string
  type: 'success' | 'warning' | 'danger' | 'info'
} {
  if (!cert.expires) {
    return { text: cert.not_after || '—', type: 'info' }
  }
  const days = Math.floor((new Date(cert.expires).getTime() - Date.now()) / 86_400_000)
  const text = formatDateTime(cert.expires)
  if (days < 0) {
    return { text: `${text}（已过期）`, type: 'danger' }
  }
  if (days < 30) {
    return { text: `${text}（${days} 天后到期）`, type: 'warning' }
  }
  return { text, type: 'success' }
}

// —— 上传证书 ——

const uploadVisible = ref(false)
const uploadSubmitting = ref(false)
const uploadFormRef = ref<FormInstance>()
const uploadForm = reactive({ name: '', cert_pem: '', key_pem: '' })

const uploadRules: FormRules<typeof uploadForm> = {
  name: [
    { required: true, message: '请输入证书名称', trigger: 'blur' },
    {
      pattern: NAME_RE,
      message: '名称需以字母或数字开头，仅可含字母、数字与 _ . @ -',
      trigger: 'blur',
    },
  ],
  cert_pem: [
    { required: true, message: '请粘贴证书 PEM 内容', trigger: 'blur' },
    {
      validator: (_r, value: string, cb) =>
        value.includes('BEGIN CERTIFICATE') ? cb() : cb(new Error('不是有效的 PEM 证书')),
      trigger: 'blur',
    },
  ],
  key_pem: [
    { required: true, message: '请粘贴私钥 PEM 内容', trigger: 'blur' },
    {
      validator: (_r, value: string, cb) =>
        value.includes('PRIVATE KEY') ? cb() : cb(new Error('不是有效的 PEM 私钥')),
      trigger: 'blur',
    },
  ],
}

function openUpload(): void {
  uploadForm.name = ''
  uploadForm.cert_pem = ''
  uploadForm.key_pem = ''
  uploadVisible.value = true
}

async function handleUpload(): Promise<void> {
  if (!uploadFormRef.value) {
    return
  }
  const valid = await uploadFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  uploadSubmitting.value = true
  try {
    await uploadCert({ ...uploadForm })
    ElMessage.success('证书已上传')
    uploadVisible.value = false
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '上传证书失败'))
  } finally {
    uploadSubmitting.value = false
  }
}

// —— 生成自签 ——

const selfVisible = ref(false)
const selfSubmitting = ref(false)
const selfFormRef = ref<FormInstance>()
const selfForm = reactive({ name: '', domain: '', days: 365 })

const selfRules: FormRules<typeof selfForm> = {
  name: [
    { required: true, message: '请输入证书名称', trigger: 'blur' },
    {
      pattern: NAME_RE,
      message: '名称需以字母或数字开头，仅可含字母、数字与 _ . @ -',
      trigger: 'blur',
    },
  ],
  domain: [
    { required: true, message: '请输入域名', trigger: 'blur' },
    { pattern: DOMAIN_RE, message: '请输入合法域名（可用 *. 通配前缀）', trigger: 'blur' },
  ],
}

function openSelf(): void {
  selfForm.name = ''
  selfForm.domain = ''
  selfForm.days = 365
  selfVisible.value = true
}

async function handleSelf(): Promise<void> {
  if (!selfFormRef.value) {
    return
  }
  const valid = await selfFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  selfSubmitting.value = true
  try {
    await generateSelfSigned({ ...selfForm })
    ElMessage.success('自签证书已生成')
    selfVisible.value = false
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '生成自签证书失败'))
  } finally {
    selfSubmitting.value = false
  }
}

// —— 删除 ——

const deleteVisible = ref(false)
const deleteLoading = ref(false)
const deleteTarget = ref<CertSummary | null>(null)

function openDelete(cert: CertSummary): void {
  deleteTarget.value = cert
  deleteVisible.value = true
}

async function handleDelete(): Promise<void> {
  const target = deleteTarget.value
  if (!target) {
    return
  }
  deleteLoading.value = true
  try {
    await deleteCert(target.name)
    ElMessage.success('证书已删除')
    deleteVisible.value = false
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '删除证书失败'))
  } finally {
    deleteLoading.value = false
  }
}
</script>

<template>
  <div class="certs-page">
    <el-card shadow="never">
      <template #header>
        <div class="certs-page__header">
          <span class="certs-page__title">证书</span>
          <div class="certs-page__actions">
            <el-button link type="primary" :loading="isPending" @click="() => refetch()"
              >刷新</el-button
            >
            <el-button @click="openSelf">生成自签</el-button>
            <el-button type="primary" @click="openUpload">上传证书</el-button>
          </div>
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
      <el-table v-else :data="certs">
        <template #empty>
          <el-empty description="还没有证书，可上传或生成自签证书" />
        </template>
        <el-table-column label="名称" min-width="140">
          <template #default="{ row }">
            <span class="certs-page__name">{{ row.name }}</span>
            <el-tag v-if="!row.has_key" size="small" type="warning" effect="plain">缺私钥</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="subject" label="主题" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.subject || '—' }}</template>
        </el-table-column>
        <el-table-column label="到期" min-width="220">
          <template #default="{ row }">
            <el-tag size="small" :type="expiryInfo(row).type" effect="plain">
              {{ expiryInfo(row).text }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="文件" min-width="140">
          <template #default="{ row }">{{ row.file }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" @click="openDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 上传证书 -->
    <el-dialog v-model="uploadVisible" title="上传证书" width="640px" :close-on-click-modal="false">
      <el-form
        ref="uploadFormRef"
        :model="uploadForm"
        :rules="uploadRules"
        label-position="top"
        @submit.prevent="handleUpload"
      >
        <el-form-item label="证书名称" prop="name">
          <el-input v-model="uploadForm.name" placeholder="如 example.com" />
        </el-form-item>
        <el-form-item label="证书 PEM（.crt）" prop="cert_pem">
          <el-input
            v-model="uploadForm.cert_pem"
            type="textarea"
            :rows="6"
            spellcheck="false"
            placeholder="-----BEGIN CERTIFICATE-----"
          />
        </el-form-item>
        <el-form-item label="私钥 PEM（.key）" prop="key_pem">
          <el-input
            v-model="uploadForm.key_pem"
            type="textarea"
            :rows="6"
            spellcheck="false"
            placeholder="-----BEGIN PRIVATE KEY-----"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploadSubmitting" @click="handleUpload">上传</el-button>
      </template>
    </el-dialog>

    <!-- 生成自签 -->
    <el-dialog
      v-model="selfVisible"
      title="生成自签证书"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form
        ref="selfFormRef"
        :model="selfForm"
        :rules="selfRules"
        label-position="top"
        @submit.prevent="handleSelf"
      >
        <el-form-item label="证书名称" prop="name">
          <el-input v-model="selfForm.name" placeholder="如 example.com" />
        </el-form-item>
        <el-form-item label="域名（CN / SAN）" prop="domain">
          <el-input v-model="selfForm.domain" placeholder="example.com" />
        </el-form-item>
        <el-form-item label="有效期（天）" prop="days">
          <el-input-number v-model="selfForm.days" :min="1" :max="3650" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="selfVisible = false">取消</el-button>
        <el-button type="primary" :loading="selfSubmitting" @click="handleSelf">生成</el-button>
      </template>
    </el-dialog>

    <!-- 删除确认 -->
    <ConfirmDangerDialog
      v-model="deleteVisible"
      title="删除证书"
      :resource-name="deleteTarget?.name ?? ''"
      message="将删除该证书及其私钥文件（引用它的站点会失效，请先调整站点配置）。"
      :loading="deleteLoading"
      @confirm="handleDelete"
    />
  </div>
</template>

<style scoped>
.certs-page__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.certs-page__title {
  font-weight: 600;
}

.certs-page__actions {
  display: flex;
  gap: 8px;
}

.certs-page__name {
  margin-right: 8px;
  font-weight: 500;
}
</style>
