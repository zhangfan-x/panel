<script setup lang="ts">
import { useRoute } from 'vue-router'
import {
  Odometer,
  Cpu,
  SetUp,
  Folder,
  Box,
  Coin,
  Connection,
  Files,
  Tickets,
  List,
  Monitor,
  Document,
  Setting,
  Platform,
  Link,
  Lock,
  Share,
  CircleCheck,
} from '@element-plus/icons-vue'

const route = useRoute()
</script>

<template>
  <div class="sidebar">
    <div class="sidebar__brand">
      <el-icon :size="22"><Odometer /></el-icon>
      <span class="sidebar__brand-text">mypanel</span>
    </div>
    <el-menu router :default-active="route.path" class="sidebar__menu">
      <el-menu-item index="/dashboard">
        <el-icon><Odometer /></el-icon>
        <span>仪表盘</span>
      </el-menu-item>
      <el-menu-item index="/host/processes">
        <el-icon><Cpu /></el-icon>
        <span>进程</span>
      </el-menu-item>
      <el-menu-item index="/host/services">
        <el-icon><SetUp /></el-icon>
        <span>服务</span>
      </el-menu-item>
      <el-menu-item index="/files">
        <el-icon><Folder /></el-icon>
        <span>文件</span>
      </el-menu-item>
      <el-sub-menu index="docker">
        <template #title>
          <el-icon><Box /></el-icon>
          <span>Docker</span>
        </template>
        <el-menu-item index="/docker/containers">
          <el-icon><Box /></el-icon>
          <span>容器</span>
        </el-menu-item>
        <el-menu-item index="/docker/images">
          <el-icon><Coin /></el-icon>
          <span>镜像</span>
        </el-menu-item>
        <el-menu-item index="/docker/networks">
          <el-icon><Connection /></el-icon>
          <span>网络</span>
        </el-menu-item>
        <el-menu-item index="/docker/volumes">
          <el-icon><Files /></el-icon>
          <span>卷</span>
        </el-menu-item>
        <el-menu-item index="/docker/compose">
          <el-icon><Tickets /></el-icon>
          <span>Compose</span>
        </el-menu-item>
        <el-menu-item index="/docker/daemon">
          <el-icon><Setting /></el-icon>
          <span>Docker 配置</span>
        </el-menu-item>
      </el-sub-menu>
      <el-sub-menu index="website">
        <template #title>
          <el-icon><Platform /></el-icon>
          <span>网站</span>
        </template>
        <el-menu-item index="/website/sites">
          <el-icon><Link /></el-icon>
          <span>站点</span>
        </el-menu-item>
        <el-menu-item index="/website/certs">
          <el-icon><Lock /></el-icon>
          <span>证书</span>
        </el-menu-item>
        <el-menu-item index="/website/firewall">
          <el-icon><CircleCheck /></el-icon>
          <span>防火墙</span>
        </el-menu-item>
      </el-sub-menu>
      <el-menu-item index="/proxy">
        <el-icon><Share /></el-icon>
        <span>代理</span>
      </el-menu-item>
      <el-menu-item index="/tasks">
        <el-icon><List /></el-icon>
        <span>任务</span>
      </el-menu-item>
      <el-menu-item index="/terminal">
        <el-icon><Monitor /></el-icon>
        <span>终端</span>
      </el-menu-item>
      <el-menu-item index="/audit">
        <el-icon><Document /></el-icon>
        <span>审计</span>
      </el-menu-item>
      <el-menu-item index="/settings">
        <el-icon><Setting /></el-icon>
        <span>设置</span>
      </el-menu-item>
    </el-menu>
  </div>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
  background-color: var(--el-menu-bg-color, #ffffff);
}

.sidebar__brand {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 60px;
  padding: 0 20px;
  font-size: 18px;
  font-weight: 600;
  color: var(--el-color-primary);
  border-bottom: 1px solid var(--el-border-color-light);
}

.sidebar__menu {
  flex: 1;
  border-right: none;
}
</style>
