<script setup lang="ts">
// 设置：个人资料 / 修改密码（需近期重认证）/ 会话管理 / 系统信息（只读）。
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import { problemMessage } from '@/api/problem'
import {
  changePassword,
  getOwnerProfile,
  getSystemSettings,
  listSessions,
  revokeSession,
} from '@/api/settings'
import type { UserSession } from '@/api/settings'
import ReauthDialog from '@/components/ReauthDialog.vue'
import { formatDateTime } from '@/composables/format'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const activeTab = ref('profile')

// —— 个人资料 ——
const {
  data: profile,
  isPending: profilePending,
  isError: profileIsError,
  error: profileError,
  refetch: refetchProfile,
} = useQuery({ queryKey: ['settings-profile'], queryFn: getOwnerProfile })

const profileErrorMessage = computed(() => problemMessage(profileError.value, '获取个人资料失败'))

// —— 会话管理 ——
const {
  data: sessions,
  isPending: sessionsPending,
  isError: sessionsIsError,
  error: sessionsError,
  isFetching: sessionsFetching,
  refetch: refetchSessions,
} = useQuery({ queryKey: ['auth-sessions'], queryFn: listSessions })

const sessionsErrorMessage = computed(() => problemMessage(sessionsError.value, '获取会话列表失败'))
const revokingId = ref<number | null>(null)

async function handleRevoke(session: UserSession): Promise<void> {
  revokingId.value = session.id
  try {
    await revokeSession(session.id)
    ElMessage.success('该会话已下线')
    await refetchSessions()
  } catch (error) {
    ElMessage.error(problemMessage(error, '下线会话失败'))
  } finally {
    revokingId.value = null
  }
}

// —— 修改密码 ——
const passwordFormRef = ref<FormInstance>()
const passwordForm = reactive({ currentPassword: '', newPassword: '', confirmPassword: '' })
const changingPassword = ref(false)
const reauthVisible = ref(false)

const passwordRules: FormRules<typeof passwordForm> = {
  currentPassword: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 12, message: '新密码至少 12 个字符', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        if (value !== passwordForm.newPassword) {
          callback(new Error('两次输入的新密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

async function handlePasswordSubmit(): Promise<void> {
  if (!passwordFormRef.value) {
    return
  }
  const valid = await passwordFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  // 修改密码要求近期重认证：窗口未开启时先弹出身份验证，成功后再提交。
  if (!auth.reauthRecent) {
    reauthVisible.value = true
    return
  }
  await doChangePassword()
}

async function doChangePassword(): Promise<void> {
  changingPassword.value = true
  try {
    await changePassword({
      current_password: passwordForm.currentPassword,
      new_password: passwordForm.newPassword,
    })
    ElMessage.success('密码修改成功，其他登录会话已全部下线')
    passwordFormRef.value?.resetFields()
    await Promise.all([refetchProfile(), refetchSessions()])
  } catch (error) {
    // 重认证窗口可能在提交前刚好过期：纠正本地状态并重新弹出身份验证。
    if ((error as ApiError).problem?.code === 'auth.reauth_required') {
      auth.reauthRecent = false
      reauthVisible.value = true
      return
    }
    ElMessage.error(problemMessage(error, '修改密码失败'))
  } finally {
    changingPassword.value = false
  }
}

function onReauthSuccess(): void {
  void doChangePassword()
}

// —— 系统信息 ——
const {
  data: system,
  isPending: systemPending,
  isError: systemIsError,
  error: systemError,
} = useQuery({ queryKey: ['settings-system'], queryFn: getSystemSettings })

const systemErrorMessage = computed(() => problemMessage(systemError.value, '获取系统信息失败'))

/** 字节数转人类可读（用于上传上限展示）。 */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return '未知'
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  const text = Number.isInteger(value) ? String(value) : value.toFixed(1)
  return `${text} ${units[index] ?? 'B'}`
}

interface DisplayItem {
  label: string
  value: string
}

const serverItems = computed<DisplayItem[]>(() => {
  const server = system.value?.server
  if (!server) {
    return []
  }
  return [
    { label: '监听地址', value: server.host },
    { label: '监听端口', value: String(server.port) },
    { label: '开发 HTTP 模式', value: server.dev_http ? '开启' : '关闭' },
    { label: 'TLS 证书路径', value: server.tls_cert },
    { label: 'TLS 私钥路径', value: server.tls_key },
  ]
})

const limitItems = computed<DisplayItem[]>(() => {
  const limits = system.value?.limits
  if (!limits) {
    return []
  }
  return [
    { label: '会话空闲超时', value: `${limits.session_idle_minutes} 分钟` },
    { label: '会话最长时长', value: `${limits.session_max_hours} 小时` },
    { label: '重认证有效窗口', value: `${limits.reauth_window_seconds} 秒` },
    { label: '登录最大尝试次数', value: `${limits.login_max_attempts} 次` },
    { label: '上传大小上限', value: formatBytes(limits.upload_max_bytes) },
    { label: '任务并发数', value: String(limits.job_concurrency) },
    { label: '审计日志保留', value: `${limits.audit_retention_days} 天` },
    { label: '监控数据保留', value: `${limits.metric_retention_days} 天` },
    { label: '终端空闲超时', value: `${limits.terminal_idle_minutes} 分钟` },
    { label: '终端最长时长', value: `${limits.terminal_max_minutes} 分钟` },
  ]
})
</script>

<template>
  <div class="settings-page">
    <el-card shadow="never">
      <template #header>
        <span class="settings-page__heading">设置</span>
      </template>

      <el-tabs v-model="activeTab">
        <el-tab-pane label="个人资料" name="profile">
          <el-skeleton v-if="profilePending" :rows="3" animated />
          <el-alert
            v-else-if="profileIsError"
            type="error"
            :title="profileErrorMessage"
            :closable="false"
            show-icon
          />
          <el-descriptions v-else-if="profile" :column="1" border class="settings-page__section">
            <el-descriptions-item label="用户名">{{ profile.username }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">
              {{ formatDateTime(profile.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="上次修改密码">
              {{ formatDateTime(profile.password_changed_at) }}
            </el-descriptions-item>
          </el-descriptions>
        </el-tab-pane>

        <el-tab-pane label="修改密码" name="password">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="修改密码属于敏感操作，需要重新验证身份；成功后其他登录会话将全部下线。"
            class="settings-page__section"
          />
          <el-form
            ref="passwordFormRef"
            :model="passwordForm"
            :rules="passwordRules"
            label-position="top"
            class="settings-page__password-form"
            @submit.prevent="handlePasswordSubmit"
          >
            <el-form-item label="当前密码" prop="currentPassword">
              <el-input
                v-model="passwordForm.currentPassword"
                type="password"
                show-password
                placeholder="请输入当前密码"
                autocomplete="current-password"
              />
            </el-form-item>
            <el-form-item label="新密码" prop="newPassword">
              <el-input
                v-model="passwordForm.newPassword"
                type="password"
                show-password
                placeholder="请输入新密码（至少 12 个字符）"
                autocomplete="new-password"
              />
            </el-form-item>
            <el-form-item label="确认新密码" prop="confirmPassword">
              <el-input
                v-model="passwordForm.confirmPassword"
                type="password"
                show-password
                placeholder="请再次输入新密码"
                autocomplete="new-password"
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="changingPassword" @click="handlePasswordSubmit">
                修改密码
              </el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="会话管理" name="sessions">
          <el-skeleton v-if="sessionsPending" :rows="4" animated />
          <el-alert
            v-else-if="sessionsIsError"
            type="error"
            :title="sessionsErrorMessage"
            :closable="false"
            show-icon
          />
          <template v-else>
            <div class="settings-page__toolbar">
              <el-button
                link
                type="primary"
                :loading="sessionsFetching"
                @click="() => refetchSessions()"
              >
                刷新
              </el-button>
            </div>
            <el-table :data="sessions ?? []" empty-text="暂无在线会话">
              <el-table-column label="会话" width="140">
                <template #default="{ row }">
                  <span># {{ row.id }}</span>
                  <el-tag v-if="row.current" size="small" type="success" class="settings-page__tag">
                    当前会话
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="创建时间" width="170">
                <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="最近活动" width="170">
                <template #default="{ row }">{{ formatDateTime(row.last_seen_at) }}</template>
              </el-table-column>
              <el-table-column label="来源 IP" width="140" prop="ip" />
              <el-table-column label="客户端" min-width="200">
                <template #default="{ row }">
                  <span class="settings-page__ua" :title="row.user_agent">{{
                    row.user_agent
                  }}</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="90" fixed="right">
                <template #default="{ row }">
                  <el-popconfirm
                    v-if="!row.current"
                    title="确定要下线该会话吗？"
                    confirm-button-text="下线"
                    cancel-button-text="取消"
                    @confirm="handleRevoke(row)"
                  >
                    <template #reference>
                      <el-button link type="danger" size="small" :loading="revokingId === row.id">
                        下线
                      </el-button>
                    </template>
                  </el-popconfirm>
                  <span v-else class="settings-page__none">—</span>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </el-tab-pane>

        <el-tab-pane label="系统信息" name="system">
          <el-skeleton v-if="systemPending" :rows="6" animated />
          <el-alert
            v-else-if="systemIsError"
            type="error"
            :title="systemErrorMessage"
            :closable="false"
            show-icon
          />
          <template v-else-if="system">
            <el-descriptions title="服务器" :column="1" border class="settings-page__section">
              <el-descriptions-item
                v-for="item in serverItems"
                :key="item.label"
                :label="item.label"
              >
                {{ item.value }}
              </el-descriptions-item>
            </el-descriptions>
            <el-descriptions title="运行限制" :column="2" border class="settings-page__section">
              <el-descriptions-item
                v-for="item in limitItems"
                :key="item.label"
                :label="item.label"
              >
                {{ item.value }}
              </el-descriptions-item>
            </el-descriptions>
            <el-descriptions title="Docker" :column="1" border class="settings-page__section">
              <el-descriptions-item label="Compose 根目录">
                {{ system.docker.compose_root }}
              </el-descriptions-item>
            </el-descriptions>
            <el-descriptions title="版本" :column="1" border class="settings-page__section">
              <el-descriptions-item label="ownpanel 版本">{{ system.version }}</el-descriptions-item>
            </el-descriptions>
          </template>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <ReauthDialog
      v-model="reauthVisible"
      message="修改密码属于敏感操作，请输入当前登录密码以确认身份。"
      @success="onReauthSuccess"
    />
  </div>
</template>

<style scoped>
.settings-page__heading {
  font-size: 16px;
  font-weight: 600;
}

.settings-page__section {
  margin-bottom: 16px;
}

.settings-page__password-form {
  max-width: 420px;
}

.settings-page__toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 8px;
}

.settings-page__tag {
  margin-left: 8px;
}

.settings-page__ua {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.settings-page__none {
  color: var(--el-text-color-placeholder);
}
</style>
