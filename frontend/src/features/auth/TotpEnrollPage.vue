<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useClipboard } from '@vueuse/core'
import { CopyDocument } from '@element-plus/icons-vue'

import { useAuthStore } from '@/stores/auth'
import { problemMessage } from '@/api/problem'

const router = useRouter()
const auth = useAuthStore()
const { copy, isSupported: clipboardSupported } = useClipboard()

const loading = ref(true)
const setupError = ref('')
const secret = ref('')
const otpauthUri = ref('')

const formRef = ref<FormInstance>()
const submitting = ref(false)
const form = reactive({ code: '' })

const rules: FormRules<typeof form> = {
  code: [
    { required: true, message: '请输入 6 位动态验证码', trigger: 'blur' },
    { pattern: /^\d{6}$/, message: '验证码为 6 位数字', trigger: 'blur' },
  ],
}

async function loadSetup(): Promise<void> {
  loading.value = true
  setupError.value = ''
  try {
    const data = await auth.setupTotp()
    secret.value = data.secret
    otpauthUri.value = data.otpauth_uri
  } catch (error) {
    setupError.value = problemMessage(error, '获取两步验证密钥失败')
  } finally {
    loading.value = false
  }
}

async function copyText(text: string, label: string): Promise<void> {
  if (!clipboardSupported.value) {
    ElMessage.warning('当前浏览器不支持自动复制，请手动选择文本')
    return
  }
  await copy(text)
  ElMessage.success(`${label}已复制到剪贴板`)
}

async function handleConfirm(): Promise<void> {
  if (!formRef.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  submitting.value = true
  try {
    await auth.confirmTotp(form.code)
    ElMessage.success('两步验证绑定成功')
    await router.push({ name: 'dashboard' })
  } catch (error) {
    ElMessage.error(problemMessage(error, '验证码错误或已过期'))
  } finally {
    submitting.value = false
  }
}

onMounted(loadSetup)
</script>

<template>
  <div class="totp-page">
    <el-card class="totp-card" shadow="never">
      <template #header>
        <div class="totp-card__header">
          <h2 class="totp-card__title">绑定两步验证</h2>
          <p class="totp-card__subtitle">
            请用 TOTP 应用扫码或手动输入密钥，然后输入生成的 6 位动态码完成绑定
          </p>
        </div>
      </template>

      <el-skeleton v-if="loading" :rows="4" animated />

      <el-alert v-else-if="setupError" type="error" :title="setupError" :closable="false" show-icon>
        <template #default>
          <el-button link type="primary" @click="loadSetup">重试</el-button>
        </template>
      </el-alert>

      <template v-else>
        <el-form label-position="top">
          <el-form-item label="密钥（Secret）">
            <el-input v-model="secret" readonly>
              <template #append>
                <el-button :icon="CopyDocument" @click="copyText(secret, '密钥')">复制</el-button>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item label="otpauth URI">
            <el-input v-model="otpauthUri" type="textarea" :rows="3" readonly resize="none" />
            <el-button
              class="totp-card__copy-uri"
              :icon="CopyDocument"
              @click="copyText(otpauthUri, 'otpauth URI')"
            >
              复制 URI
            </el-button>
          </el-form-item>
        </el-form>

        <el-divider />

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-position="top"
          @submit.prevent="handleConfirm"
        >
          <el-form-item label="动态验证码" prop="code">
            <el-input
              v-model="form.code"
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
              class="totp-card__submit"
              @click="handleConfirm"
            >
              确认绑定
            </el-button>
          </el-form-item>
        </el-form>
      </template>
    </el-card>
  </div>
</template>

<style scoped>
.totp-page {
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 24px;
}

.totp-card {
  width: 100%;
  max-width: 520px;
}

.totp-card__title {
  margin: 0;
  font-size: 20px;
}

.totp-card__subtitle {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.totp-card__copy-uri {
  margin-top: 8px;
}

.totp-card__submit {
  width: 100%;
}
</style>
