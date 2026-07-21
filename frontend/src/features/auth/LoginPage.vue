<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'

import { useAuthStore } from '@/stores/auth'
import { problemMessage } from '@/api/problem'
import type { SessionState } from '@/api/types'

const router = useRouter()
const auth = useAuthStore()

type Step = 'password' | 'totp'
const step = ref<Step>('password')
const submitting = ref(false)

const passwordFormRef = ref<FormInstance>()
const totpFormRef = ref<FormInstance>()

const passwordForm = reactive({ username: '', password: '' })
const totpForm = reactive({ code: '' })

const passwordRules: FormRules<typeof passwordForm> = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

const totpRules: FormRules<typeof totpForm> = {
  code: [
    { required: true, message: '请输入 6 位动态验证码', trigger: 'blur' },
    { pattern: /^\d{6}$/, message: '验证码为 6 位数字', trigger: 'blur' },
  ],
}

// 刷新后若会话仍处于 mfa_pending，保持停留在 TOTP 步骤。
onMounted(() => {
  if (auth.sessionState === 'mfa_pending') {
    step.value = 'totp'
  }
})

async function routeByState(state: SessionState): Promise<void> {
  if (state === 'active') {
    await router.push({ name: 'dashboard' })
  } else if (state === 'totp_enroll') {
    await router.push({ name: 'totp-enroll' })
  } else {
    step.value = 'totp'
  }
}

async function handlePasswordSubmit(): Promise<void> {
  if (!passwordFormRef.value) {
    return
  }
  const valid = await passwordFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  submitting.value = true
  try {
    const state = await auth.login(passwordForm.username, passwordForm.password)
    await routeByState(state)
  } catch (error) {
    ElMessage.error(problemMessage(error, '用户名或密码错误'))
  } finally {
    submitting.value = false
  }
}

async function handleTotpSubmit(): Promise<void> {
  if (!totpFormRef.value) {
    return
  }
  const valid = await totpFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  submitting.value = true
  try {
    const state = await auth.verifyTotp(totpForm.code)
    if (state === 'active') {
      await router.push({ name: 'dashboard' })
    } else {
      await routeByState(state)
    }
  } catch (error) {
    ElMessage.error(problemMessage(error, '验证码错误或已过期'))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <el-card class="auth-card" shadow="never">
      <template #header>
        <div class="auth-card__header">
          <h2 class="auth-card__title">登录 mypanel</h2>
          <p class="auth-card__subtitle">
            {{ step === 'password' ? '请输入管理员账号与密码' : '请输入两步验证动态码' }}
          </p>
        </div>
      </template>

      <el-form
        v-if="step === 'password'"
        ref="passwordFormRef"
        :model="passwordForm"
        :rules="passwordRules"
        label-position="top"
        @submit.prevent="handlePasswordSubmit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="passwordForm.username" autocomplete="username" placeholder="用户名" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="passwordForm.password"
            type="password"
            show-password
            autocomplete="current-password"
            placeholder="密码"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="submitting"
            class="auth-card__submit"
            @click="handlePasswordSubmit"
          >
            下一步
          </el-button>
        </el-form-item>
      </el-form>

      <el-form
        v-else
        ref="totpFormRef"
        :model="totpForm"
        :rules="totpRules"
        label-position="top"
        @submit.prevent="handleTotpSubmit"
      >
        <el-form-item label="动态验证码" prop="code">
          <el-input
            v-model="totpForm.code"
            maxlength="6"
            inputmode="numeric"
            autocomplete="one-time-code"
            placeholder="TOTP 应用中的 6 位码"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="submitting"
            class="auth-card__submit"
            @click="handleTotpSubmit"
          >
            登录
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.auth-page {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
  padding: 24px;
}

.auth-card {
  width: 100%;
  max-width: 420px;
}

.auth-card__title {
  margin: 0;
  font-size: 20px;
}

.auth-card__subtitle {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.auth-card__submit {
  width: 100%;
}
</style>
