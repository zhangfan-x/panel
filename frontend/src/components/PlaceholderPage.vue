<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => (route.meta.title as string | undefined) ?? '该功能')
</script>

<template>
  <div class="placeholder-page">
    <el-result icon="info" :title="title" sub-title="该功能将在后续版本提供">
      <template #extra>
        <el-tag type="info" effect="plain">敬请期待</el-tag>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.placeholder-page {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 60vh;
}
</style>
