<script setup lang="ts">
import SidebarMenu from '@/components/SidebarMenu.vue'
import AppTopBar from '@/components/AppTopBar.vue'
</script>

<template>
  <el-container class="app-layout">
    <el-aside width="220px" class="app-layout__aside">
      <SidebarMenu />
    </el-aside>
    <el-container class="app-layout__body">
      <el-header height="60px" class="app-layout__header">
        <AppTopBar />
      </el-header>
      <el-main class="app-layout__main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-layout {
  height: 100vh;
}

.app-layout__aside {
  border-right: 1px solid var(--el-border-color-light);
  overflow: hidden;
}

.app-layout__header {
  background-color: var(--el-bg-color, #ffffff);
  border-bottom: 1px solid var(--el-border-color-light);
  padding: 0;
}

.app-layout__main {
  background-color: var(--el-bg-color-page, #f2f3f5);
  padding: 20px;
  overflow: auto;
}
</style>
