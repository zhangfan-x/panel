<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { client } from '@/api/client'
import { problemMessage } from '@/api/problem'
import { useAuthStore } from '@/stores/auth'

/**
 * 重认证对话框：敏感操作被 403 auth.reauth_required 拒绝时弹出，
 * 用户重输密码经 POST /auth/reauth 开启重认证窗口，成功后发出 success，
 * 调用方即可重试刚才被拦下的操作。
 */
const props = withDefaults(
  defineProps<{
    /** 对话框可见性（v-model）。 */
    modelValue: boolean
    /** 对话框标题。 */
    title?: string
    /** 说明文案，告知用户为何需要再次验证。 */
    message?: string
  }>(),
  {
    title: '验证身份',
    message: '该操作较为敏感，请输入当前登录密码以确认身份。',
  },
)

const emit = defineEmits<{
  /** 同步 v-model 可见性（关闭按钮 / ESC 也会触发 false）。 */
  (e: 'update:modelValue', value: boolean): void
  /** 密码验证成功，重认证窗口已开启；调用方可立即重试受保护操作。 */
  (e: 'success'): void
  /** 用户点击取消按钮（随后会同步 v-model 为 false）。 */
  (e: 'cancel'): void
}>()

const auth = useAuthStore()
const password = ref('')
const submitting = ref(false)

// 每次打开时清空上次输入的密码。
watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      password.value = ''
    }
  },
)

function onVisibleChange(value: boolean): void {
  emit('update:modelValue', value)
}

function handleCancel(): void {
  emit('cancel')
  emit('update:modelValue', false)
}

async function handleSubmit(): Promise<void> {
  if (!password.value || submitting.value) {
    return
  }
  submitting.value = true
  try {
    await client.post('/auth/reauth', { password: password.value })
    // 与服务端状态对齐，让依赖 store 的界面（如设置页）立即感知重认证窗口已开启。
    auth.reauthRecent = true
    emit('success')
    emit('update:modelValue', false)
  } catch (error) {
    // 验证失败：清空密码、保持对话框打开，等待用户重试。
    password.value = ''
    ElMessage.error(problemMessage(error, '密码验证失败，请重试'))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="420px"
    :close-on-click-modal="false"
    @update:model-value="onVisibleChange"
  >
    <p class="reauth-dialog__message">{{ message }}</p>
    <el-form @submit.prevent="handleSubmit">
      <el-form-item>
        <el-input
          v-model="password"
          type="password"
          show-password
          placeholder="请输入登录密码"
          autocomplete="current-password"
          @keyup.enter="handleSubmit"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="handleCancel">取消</el-button>
      <el-button type="primary" :disabled="!password" :loading="submitting" @click="handleSubmit">
        确认
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.reauth-dialog__message {
  margin: 0 0 12px;
  color: var(--el-text-color-regular);
  font-size: 14px;
}
</style>
