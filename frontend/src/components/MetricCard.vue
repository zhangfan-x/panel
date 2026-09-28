<script setup lang="ts">
import { computed } from 'vue'

/**
 * 紧凑指标卡：标题 + 主数值(+单位)，可选右侧百分比环形指示。
 * 环形颜色按严重程度着色（<70 主色、≥70 警告、≥90 危险），数值文本始终可见，
 * 不单靠颜色传达信息。默认插槽可放补充说明（如趋势、副指标）。
 */
const props = withDefaults(
  defineProps<{
    /** 卡片标题（指标名，如「CPU 使用率」）。 */
    title: string
    /** 主数值（调用方格式化好的字符串或数字）。 */
    value: string | number
    /** 数值单位（如 '%'、'GiB'），显示在数值右侧的弱化文本。 */
    unit?: string
    /** 百分比（0–100）；传入时显示右侧环形进度，超出范围会被钳制。 */
    percent?: number
  }>(),
  { unit: '' },
)

/** 是否显示环形进度（仅在传入了有限数值时）。 */
const showRing = computed(() => props.percent !== undefined && Number.isFinite(props.percent))

/** 环形进度取值：钳制到 0–100，避免异常数据撑破组件。 */
const ringPercent = computed(() => {
  if (!showRing.value) {
    return 0
  }
  return Math.min(100, Math.max(0, props.percent ?? 0))
})

/** 严重程度着色：Element Plus 状态色，仅表达状态，不用作序列色。 */
const ringColor = computed(() => {
  if (ringPercent.value >= 90) {
    return '#f56c6c'
  }
  if (ringPercent.value >= 70) {
    return '#e6a23c'
  }
  return '#409eff'
})

/** 环内文本取整显示，避免小数在小尺寸环里溢出。 */
function formatRing(p: number): string {
  return `${Math.round(p)}%`
}
</script>

<template>
  <el-card shadow="never" class="metric-card">
    <div class="metric-card__row">
      <div class="metric-card__main">
        <div class="metric-card__title">{{ title }}</div>
        <div class="metric-card__value-row">
          <span class="metric-card__value">{{ value }}</span>
          <span v-if="unit" class="metric-card__unit">{{ unit }}</span>
        </div>
        <div v-if="$slots.default" class="metric-card__extra">
          <slot />
        </div>
      </div>
      <el-progress
        v-if="showRing"
        type="circle"
        :percentage="ringPercent"
        :width="64"
        :stroke-width="6"
        :color="ringColor"
        :format="formatRing"
        class="metric-card__ring"
      />
    </div>
  </el-card>
</template>

<style scoped>
.metric-card__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.metric-card__main {
  min-width: 0;
}

.metric-card__title {
  margin-bottom: 6px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.metric-card__value-row {
  display: flex;
  align-items: baseline;
  gap: 4px;
}

.metric-card__value {
  color: var(--el-text-color-primary);
  font-size: 26px;
  font-weight: 600;
  line-height: 1.2;
}

.metric-card__unit {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.metric-card__extra {
  margin-top: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.metric-card__ring {
  flex-shrink: 0;
}
</style>
