<script setup lang="ts">
import { computed, onMounted, shallowRef } from 'vue'
import type { Component } from 'vue'
import type { EChartsOption } from 'echarts'

/** 单条时间序列；data 为 [时间(epoch 毫秒或 ISO 字符串), 数值] 点列。 */
export interface TimeSeriesSeriesData {
  /** 序列名（图例与 tooltip 中显示）。 */
  name: string
  /** 按时间升序排列的数据点。 */
  data: Array<[number | string, number]>
}

/**
 * 时间序列折线图（懒加载 ECharts）：echarts / vue-echarts 只在本组件首次
 * 挂载时动态 import，构建产物落入独立的 'echarts' 分包，不拖慢首屏。
 */
const props = withDefaults(
  defineProps<{
    /** 序列列表；多于 1 条时自动显示图例。 */
    series: TimeSeriesSeriesData[]
    /** 图表高度（px）。 */
    height?: number
    /** 数值单位（追加在 y 轴刻度与 tooltip 数值后，如 '%'、'MB/s'）。 */
    unit?: string
    /** y 轴固定上限（百分比图传 100）；不传则自适应。 */
    yMax?: number
    /** 是否绘制 10% 透明度面积填充（单序列趋势图更易读）。 */
    area?: boolean
  }>(),
  { height: 260, unit: '', yMax: undefined, area: false },
)

/**
 * 固定顺序的分类色板（OKLab 色觉模拟校验通过：相邻序列在色觉障碍模拟下
 * 仍可区分）。按序取用、不得循环；文本与图例用文本色，不用序列色。
 */
const SERIES_COLORS = [
  '#2a78d6',
  '#008300',
  '#e87ba4',
  '#eda100',
  '#1baf7a',
  '#eb6834',
  '#4a3aa7',
  '#e34948',
]

/** 懒加载完成后的 vue-echarts 组件；加载前渲染骨架屏。 */
const chartComp = shallowRef<Component | null>(null)

onMounted(async () => {
  const [
    { use },
    { LineChart },
    { GridComponent, LegendComponent, TooltipComponent },
    { CanvasRenderer },
    vchartModule,
  ] = await Promise.all([
    import('echarts/core'),
    import('echarts/charts'),
    import('echarts/components'),
    import('echarts/renderers'),
    import('vue-echarts'),
  ])
  // use() 幂等，重复注册无副作用。
  use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])
  chartComp.value = vchartModule.default
})

/** tooltip 数值：最多保留 2 位小数并附加单位。 */
function formatValue(value: unknown): string {
  if (typeof value === 'number') {
    return `${Math.round(value * 100) / 100}${props.unit}`
  }
  return `${String(value ?? '-')}${props.unit}`
}

const option = computed<EChartsOption>(() => ({
  animation: false,
  color: SERIES_COLORS,
  tooltip: {
    trigger: 'axis',
    confine: true,
    valueFormatter: (value: unknown) => formatValue(value),
  },
  // 单序列不显示图例（标题已说明画的是什么），多序列必须有图例。
  legend:
    props.series.length > 1
      ? {
          top: 0,
          left: 'center',
          icon: 'roundRect',
          itemWidth: 12,
          itemHeight: 4,
          textStyle: { color: '#606266', fontSize: 12 },
        }
      : undefined,
  grid: {
    left: 8,
    right: 16,
    top: props.series.length > 1 ? 32 : 16,
    bottom: 0,
    containLabel: true,
  },
  xAxis: {
    type: 'time',
    axisLine: { lineStyle: { color: '#dcdfe6' } },
    axisTick: { show: false },
    axisLabel: { hideOverlap: true, color: '#909399', fontSize: 11 },
    splitLine: { show: false },
  },
  yAxis: {
    type: 'value',
    max: props.yMax,
    axisLine: { show: false },
    axisLabel: {
      color: '#909399',
      fontSize: 11,
      formatter: (v: number) => `${v}${props.unit}`,
    },
    splitLine: { lineStyle: { color: '#ebeef5', width: 1 } },
  },
  series: props.series.map((s) => ({
    name: s.name,
    type: 'line' as const,
    data: s.data,
    showSymbol: false,
    smooth: false,
    lineStyle: { width: 2, cap: 'round' as const, join: 'round' as const },
    areaStyle: props.area ? { opacity: 0.1 } : undefined,
    emphasis: { focus: 'series' as const },
  })),
}))
</script>

<template>
  <div class="ts-chart" :style="{ height: `${height}px` }">
    <component
      :is="chartComp"
      v-if="chartComp"
      class="ts-chart__canvas"
      :option="option"
      autoresize
    />
    <el-skeleton v-else :rows="3" animated />
  </div>
</template>

<style scoped>
.ts-chart {
  position: relative;
  width: 100%;
}

.ts-chart__canvas {
  width: 100%;
  height: 100%;
}
</style>
