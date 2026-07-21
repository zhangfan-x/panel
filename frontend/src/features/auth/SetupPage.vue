<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'

import { useAuthStore } from '@/stores/auth'
import { problemMessage } from '@/api/problem'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const submitting = ref(false)

const form = reactive({
  token: typeof route.query.token === 'string' ? route.query.token : '',
  username: '',
  password: '',
  confirmPassword: '',
})

// 令牌若来自 URL query，读取后立即从地址栏清除，避免残留在浏览器历史。
onMounted(() => {
  if (route.query.token !== undefined) {
    void router.replace({ path: route.path, query: {} })
  }
})

const rules: FormRules<typeof form> = {
  token: [{ required: true, message: '请输入初始化令牌', trigger: 'blur' }],
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 32, message: '用户名长度需在 3 到 32 个字符之间', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 12, message: '密码至少 12 个字符', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        if (value !== form.password) {
          callback(new Error('两次输入的密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

async function handleSubmit(): Promise<void> {
  if (!formRef.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  submitting.value = true
  try {
    await auth.createOwner({
      token: form.token,
      username: form.username,
      password: form.password,
    })
    ElMessage.success('初始化成功，请继续绑定两步验证')
    await router.push({ name: 'totp-enroll' })
  } catch (error) {
    ElMessage.error(problemMessage(error, '初始化失败，请检查令牌是否有效'))
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
          <h2 class="auth-card__title">初始化 mypanel</h2>
          <p class="auth-card__subtitle">使用安装时生成的一次性令牌创建管理员账户</p>
        </div>
      </template>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="handleSubmit"
      >
        <el-form-item label="初始化令牌" prop="token">
          <el-input v-model="form.token" placeholder="安装脚本输出的令牌" />
        </el-form-item>
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="管理员用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="至少 12 个字符"
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirmPassword">
          <el-input
            v-model="form.confirmPassword"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="再次输入密码"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="submitting"
            class="auth-card__submit"
            @click="handleSubmit"
          >
            创建管理员
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
