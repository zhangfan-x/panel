<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { SwitchButton, User } from '@element-plus/icons-vue'

import { useAuthStore } from '@/stores/auth'
import { problemMessage } from '@/api/problem'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const pageTitle = computed(() => (route.meta.title as string | undefined) ?? 'ownpanel')
const username = computed(() => auth.user?.username ?? '未登录')

async function handleLogout(): Promise<void> {
  try {
    await auth.logout()
  } catch (error) {
    // 退出即便失败也已在本地清空会话，仅提示后端返回的问题。
    ElMessage.warning(problemMessage(error, '退出登录时发生问题'))
  } finally {
    await router.push({ name: 'login' })
  }
}
</script>

<template>
  <div class="topbar">
    <div class="topbar__title">{{ pageTitle }}</div>
    <div class="topbar__right">
      <el-dropdown trigger="click">
        <span class="topbar__user">
          <el-icon><User /></el-icon>
          <span class="topbar__username">{{ username }}</span>
        </span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item :icon="SwitchButton" @click="handleLogout">退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
  </div>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 100%;
  padding: 0 20px;
}

.topbar__title {
  font-size: 16px;
  font-weight: 600;
}

.topbar__user {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  outline: none;
}

.topbar__username {
  font-size: 14px;
}
</style>
