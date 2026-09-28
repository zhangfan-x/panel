<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'

import { problemMessage } from '@/api/problem'
import { getMetrics, getOverview } from '@/api/system'
import type { MetricSample } from '@/api/system'
import { JOB_STATUS_META } from '@/api/jobs'
import type { JobStatusMeta, JobTagType } from '@/api/jobs'
import MetricCard from '@/components/MetricCard.vue'
import TimeSeriesChart from '@/components/TimeSeriesChart.vue'
import type { TimeSeriesSeriesData } from '@/components/TimeSeriesChart.vue'
import { formatDateTime, formatUptime } from '@/composables/format'
import { useEventStream } from '@/composables/useEventStream'

/** 图表缓冲的硬上限点数（防止长窗口 + 5s 实时追加导致内存无限增长）。 */
const HARD_MAX_POINTS = 12000

/** 图表时间段选项（分钟数 → 展示文案）。 */
const RANGE_OPTIONS = [
  { minutes: 60, label: '1 小时' },
  { minutes: 360, label: '6 小时' },
  { minutes: 1440, label: '24 小时' },
  { minutes: 10080, label: '7 天' },
]

// —— 首屏聚合（主机/服务摘要/最近任务/安全事件），30s 自动刷新兜底。 ——
const {
  data: overview,
  isPending,
  isError,
  error,
  refetch,
} = useQuery({
  queryKey: ['system-overview'],
  queryFn: getOverview,
  refetchInterval: 30_000,
})

const errorMessage = computed(() => problemMessage(error.value, '获取系统概览失败'))

// —— 指标时间序列：历史采样打底 + 全局 SSE 实时追加。 ——
const samples = shallowRef<MetricSample[]>([])

/** 当前选中的图表时间段（分钟），默认 24 小时。 */
const selectedMinutes = ref(1440)

// 切换时间段时立刻清空缓冲（sync 确保先于新窗口历史数据并入），避免不同窗口的数据混杂。
watch(
  selectedMinutes,
  () => {
    samples.value = []
  },
  { flush: 'sync' },
)

/** 按所选窗口起点裁剪缓冲：丢弃窗口外的旧点，并施加硬上限。
 *
 * 窗口内一个点都没有时保留最新一点，让图表与指标卡不至于空白。
 */
function trimToWindow(buf: MetricSample[]): MetricSample[] {
  const cutoffMs = Date.now() - selectedMinutes.value * 60_000
  const start = buf.findIndex((s) => Date.parse(s.ts) >= cutoffMs)
  const windowed = start === -1 ? buf.slice(-1) : start === 0 ? buf : buf.slice(start)
  return windowed.length > HARD_MAX_POINTS ? windowed.slice(-HARD_MAX_POINTS) : windowed
}

const { data: history } = useQuery({
  queryKey: ['system-metrics', selectedMinutes],
  queryFn: () => getMetrics(selectedMinutes.value),
})

/** 历史采样并入缓冲：只补在现有实时数据之前，避免乱序与重复。 */
watch(history, (value) => {
  if (!value || value.samples.length === 0) {
    return
  }
  const live = samples.value
  const firstLiveTs = live[0]?.ts
  const merged =
    firstLiveTs === undefined
      ? [...value.samples]
      : [...value.samples.filter((s) => s.ts < firstLiveTs), ...live]
  samples.value = trimToWindow(merged)
})

const { connected, onMetrics } = useEventStream()

/** 判断 SSE 负载确实是一次采样（防御异常帧）。 */
function isMetricSample(value: unknown): value is MetricSample {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { ts?: unknown }).ts === 'string' &&
    typeof (value as { cpu_percent?: unknown }).cpu_percent === 'number'
  )
}

/** 最近一次「实时」采样（仅由 SSE 更新，绝不来自历史行）。 */
const liveSample = shallowRef<MetricSample | null>(null)

onMetrics((raw) => {
  if (!isMetricSample(raw)) {
    return
  }
  liveSample.value = raw
  const buf = samples.value
  const last = buf[buf.length - 1]
  if (last !== undefined && last.ts >= raw.ts) {
    return
  }
  samples.value = trimToWindow([...buf, raw])
})

/**
 * 指标卡与磁盘卡的数据源：只用实时样本（SSE 最新一帧，退回 overview 携带的
 * 最近一次采样）。绝不使用图表缓冲里的历史聚合行——旧历史行的磁盘列表可能
 * 是伪文件系统过滤之前存下的，切换时间段时会瞬间刷出大量磁盘卡片。
 */
const cardSample = computed<MetricSample | null>(
  () => liveSample.value ?? overview.value?.metrics ?? null,
)

// —— 数值格式化（页面内展示用）。 ——

/** 按二进制单位格式化字节数（B/KiB/MiB/GiB/TiB）。 */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return '—'
  }
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  let value = bytes
  let idx = 0
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024
    idx += 1
  }
  const text = idx === 0 ? String(Math.round(value)) : value.toFixed(1)
  return `${text} ${units[idx] ?? 'B'}`
}

/** 字节速率（追加 /s）。 */
function formatRate(bytesPerSecond: number): string {
  return `${formatBytes(bytesPerSecond)}/s`
}

/** 保留 2 位小数（图表数据点用）。 */
function round2(value: number): number {
  return Math.round(value * 100) / 100
}

// —— 指标卡行：CPU / 内存 / Swap / 负载 / 网络 / 运行时长。 ——

interface CardVm {
  key: string
  title: string
  value: string
  unit?: string
  percent?: number
  extra?: string
}

const metricCards = computed<CardVm[]>(() => {
  const s = cardSample.value
  const host = overview.value?.host
  return [
    {
      key: 'cpu',
      title: 'CPU 使用率',
      value: s ? s.cpu_percent.toFixed(1) : '—',
      unit: '%',
      percent: s?.cpu_percent,
      extra: s ? `${s.per_cpu.length || (host?.cpu_count ?? 0)} 核` : undefined,
    },
    {
      key: 'mem',
      title: '内存',
      value: s ? formatBytes(s.mem.used) : '—',
      percent: s?.mem.percent,
      extra: s ? `共 ${formatBytes(s.mem.total)}` : undefined,
    },
    {
      key: 'swap',
      title: 'Swap',
      value: s ? (s.swap.total > 0 ? formatBytes(s.swap.used) : '未启用') : '—',
      percent: s && s.swap.total > 0 ? s.swap.percent : undefined,
      extra: s && s.swap.total > 0 ? `共 ${formatBytes(s.swap.total)}` : undefined,
    },
    {
      key: 'load',
      title: '系统负载',
      value: s?.load[0] !== undefined ? s.load[0].toFixed(2) : '—',
      extra: s
        ? `5/15 分钟：${(s.load[1] ?? 0).toFixed(2)} / ${(s.load[2] ?? 0).toFixed(2)}`
        : undefined,
    },
    {
      key: 'net',
      title: '网络速率',
      value: s ? formatRate(s.net.rx_bytes_per_s) : '—',
      unit: '下行',
      extra: s ? `上行 ${formatRate(s.net.tx_bytes_per_s)}` : undefined,
    },
    {
      key: 'uptime',
      title: '运行时长',
      value: host ? formatUptime(host.uptime_s) : '—',
      extra: host ? `主机 ${host.hostname}` : undefined,
    },
  ]
})

/** 磁盘明细（详情对话框用）与合并卡的汇总。 */
const disks = computed(() => cardSample.value?.disks ?? [])

const diskSummary = computed(() => {
  const list = disks.value
  const total = list.reduce((acc, d) => acc + d.total, 0)
  const used = list.reduce((acc, d) => acc + d.used, 0)
  return {
    count: list.length,
    total,
    used,
    percent: total > 0 ? (used / total) * 100 : 0,
  }
})

/** GPU 卡片与图表（无 GPU 或旧历史行时为空）。 */
const gpus = computed(() => cardSample.value?.gpus ?? [])
const hasGpu = computed(() => gpus.value.length > 0)

// —— 时间序列图表数据。 ——

const cpuSeries = computed<TimeSeriesSeriesData[]>(() => [
  {
    name: 'CPU',
    data: samples.value.map((s): [string, number] => [s.ts, round2(s.cpu_percent)]),
  },
])

const memSeries = computed<TimeSeriesSeriesData[]>(() => [
  {
    name: '内存',
    data: samples.value.map((s): [string, number] => [s.ts, round2(s.mem.percent)]),
  },
  {
    name: 'Swap',
    data: samples.value.map((s): [string, number] => [s.ts, round2(s.swap.percent)]),
  },
])

const netSeries = computed<TimeSeriesSeriesData[]>(() => [
  {
    name: '下行',
    data: samples.value.map((s): [string, number] => [
      s.ts,
      round2(s.net.rx_bytes_per_s / 1048576),
    ]),
  },
  {
    name: '上行',
    data: samples.value.map((s): [string, number] => [
      s.ts,
      round2(s.net.tx_bytes_per_s / 1048576),
    ]),
  },
])

/** GPU 图表：每块 GPU 的利用率与显存占用百分比（旧历史行无 gpus 字段，跳过）。 */
const gpuSeries = computed<TimeSeriesSeriesData[]>(() =>
  gpus.value.flatMap((gpu, i) => {
    const points = samples.value.filter((s) => s.gpus?.[i] !== undefined)
    const prefix = gpus.value.length > 1 ? `GPU${gpu.index} ` : ''
    return [
      {
        name: `${prefix}利用率`,
        data: points.map((s): [string, number] => [s.ts, round2(s.gpus![i]!.util_percent)]),
      },
      {
        name: `${prefix}显存`,
        data: points.map((s): [string, number] => [
          s.ts,
          s.gpus![i]!.mem_total_mb > 0
            ? round2((s.gpus![i]!.mem_used_mb / s.gpus![i]!.mem_total_mb) * 100)
            : 0,
        ]),
      },
    ]
  }),
)

// —— 图表详情对话框：点击图表卡片查看更细粒度的数据。 ——

type DetailKey = 'cpu' | 'mem' | 'net' | 'gpu' | 'disk'

const detailKey = ref<DetailKey | null>(null)

const DETAIL_TITLES: Record<DetailKey, string> = {
  cpu: 'CPU 详情',
  mem: '内存 / Swap 详情',
  net: '网络详情',
  gpu: 'GPU 详情',
  disk: '磁盘详情',
}

function openDetail(key: DetailKey): void {
  detailKey.value = key
}

/** 每核使用率曲线（核数超过色板时 ECharts 自动循环取色，可接受）。 */
const perCpuSeries = computed<TimeSeriesSeriesData[]>(() => {
  const coreCount = cardSample.value?.per_cpu.length ?? 0
  return Array.from({ length: coreCount }, (_, i) => ({
    name: `核心 ${i}`,
    data: samples.value
      .filter((s) => s.per_cpu[i] !== undefined)
      .map((s): [string, number] => [s.ts, round2(s.per_cpu[i]!)]),
  }))
})

/** 1/5/15 分钟平均负载曲线。 */
const loadSeries = computed<TimeSeriesSeriesData[]>(() =>
  ([0, 1, 2] as const).map((i) => ({
    name: ['1 分钟', '5 分钟', '15 分钟'][i]!,
    data: samples.value
      .filter((s) => s.load[i] !== undefined)
      .map((s): [string, number] => [s.ts, round2(s.load[i]!)]),
  })),
)

/** 内存绝对量曲线（GiB）。 */
const memAbsSeries = computed<TimeSeriesSeriesData[]>(() => [
  {
    name: '内存已用',
    data: samples.value.map((s): [string, number] => [s.ts, round2(s.mem.used / 1073741824)]),
  },
  {
    name: '内存可用',
    data: samples.value.map((s): [string, number] => [s.ts, round2(s.mem.available / 1073741824)]),
  },
  {
    name: 'Swap 已用',
    data: samples.value.map((s): [string, number] => [s.ts, round2(s.swap.used / 1073741824)]),
  },
])

/** 详情对话框底部的当前值明细。 */
const detailItems = computed<{ label: string; value: string }[]>(() => {
  const s = cardSample.value
  if (!s || detailKey.value === null) {
    return []
  }
  switch (detailKey.value) {
    case 'cpu':
      return [
        { label: '总使用率', value: `${s.cpu_percent.toFixed(1)}%` },
        { label: '核心数', value: String(s.per_cpu.length) },
        {
          label: '最忙核心',
          value:
            s.per_cpu.length > 0
              ? `核心 ${s.per_cpu.indexOf(Math.max(...s.per_cpu))}（${Math.max(...s.per_cpu).toFixed(1)}%）`
              : '—',
        },
        {
          label: '负载 1/5/15 分钟',
          value: s.load.map((v) => v.toFixed(2)).join(' / '),
        },
      ]
    case 'mem':
      return [
        { label: '内存总量', value: formatBytes(s.mem.total) },
        { label: '已用', value: `${formatBytes(s.mem.used)}（${s.mem.percent.toFixed(1)}%）` },
        { label: '可用', value: formatBytes(s.mem.available) },
        {
          label: 'Swap',
          value:
            s.swap.total > 0
              ? `${formatBytes(s.swap.used)} / ${formatBytes(s.swap.total)}（${s.swap.percent.toFixed(1)}%）`
              : '未启用',
        },
      ]
    case 'net':
      return [
        { label: '下行速率', value: formatRate(s.net.rx_bytes_per_s) },
        { label: '上行速率', value: formatRate(s.net.tx_bytes_per_s) },
        { label: '累计接收', value: formatBytes(s.net.rx_total) },
        { label: '累计发送', value: formatBytes(s.net.tx_total) },
      ]
    case 'gpu':
      return gpus.value.flatMap((gpu) => [
        { label: `GPU${gpu.index} 型号`, value: gpu.name },
        { label: `GPU${gpu.index} 利用率`, value: `${gpu.util_percent.toFixed(0)}%` },
        {
          label: `GPU${gpu.index} 显存`,
          value: `${(gpu.mem_used_mb / 1024).toFixed(1)} / ${(gpu.mem_total_mb / 1024).toFixed(1)} GiB`,
        },
        { label: `GPU${gpu.index} 温度`, value: `${gpu.temp_c.toFixed(0)}°C` },
      ])
    default:
      return []
  }
})

// —— 状态徽章与列表展示。 ——

/** Docker 徽章文案。 */
const dockerChip = computed(() => {
  const d = overview.value?.docker
  if (!d || !d.available) {
    return { text: 'Docker 不可用', type: 'info' as const }
  }
  return { text: `Docker 运行 ${d.running} / ${d.total}`, type: 'success' as const }
})

/** systemd 徽章文案（有失败单元时标红）。 */
const systemdChip = computed(() => {
  const s = overview.value?.systemd
  if (!s || !s.available) {
    return { text: 'systemd 不可用', type: 'info' as const }
  }
  if (s.failed > 0) {
    return { text: `systemd 失败单元 ${s.failed}`, type: 'danger' as const }
  }
  return { text: 'systemd 正常', type: 'success' as const }
})

/** 任务状态展示元数据（未知状态兜底为原样文本）。 */
function jobStatusMeta(status: string): JobStatusMeta {
  const meta = (JOB_STATUS_META as Record<string, JobStatusMeta | undefined>)[status]
  return meta ?? { text: status, tagType: 'info' }
}

/** 安全事件结果着色：denied 红、error 黄、其余灰。 */
function resultTagType(result: string): JobTagType {
  if (result === 'denied') {
    return 'danger'
  }
  if (result === 'error') {
    return 'warning'
  }
  return 'info'
}

/** 可空时间字符串格式化。 */
function formatMaybeTime(value: string | null): string {
  return value ? formatDateTime(value) : '—'
}

/** 主机信息描述列表。 */
const hostItems = computed(() => {
  const host = overview.value?.host
  if (!host) {
    return []
  }
  return [
    { label: '主机名', value: host.hostname },
    { label: '操作系统', value: host.os_release || '未知' },
    { label: '内核', value: host.kernel },
    { label: '架构', value: host.arch },
    { label: 'CPU 核数', value: String(host.cpu_count) },
    { label: '运行时长', value: formatUptime(host.uptime_s) },
  ]
})
</script>

<template>
  <div class="dashboard">
    <div class="dashboard__toolbar">
      <div class="dashboard__chips">
        <el-tag :type="connected ? 'success' : 'info'" effect="plain">
          {{ connected ? '实时推送已连接' : '实时推送连接中' }}
        </el-tag>
        <el-tag v-if="overview" :type="dockerChip.type" effect="plain">{{
          dockerChip.text
        }}</el-tag>
        <el-tag v-if="overview" :type="systemdChip.type" effect="plain">
          {{ systemdChip.text }}
        </el-tag>
      </div>
      <el-button link type="primary" :loading="isPending" @click="() => refetch()">刷新</el-button>
    </div>

    <el-skeleton v-if="isPending" :rows="6" animated />

    <el-alert v-else-if="isError" type="error" :title="errorMessage" :closable="false" show-icon />

    <template v-else>
      <el-row :gutter="16">
        <el-col
          v-for="card in metricCards"
          :key="card.key"
          :xs="12"
          :sm="12"
          :lg="6"
          class="dashboard__col"
        >
          <MetricCard
            :title="card.title"
            :value="card.value"
            :unit="card.unit"
            :percent="card.percent"
          >
            <template v-if="card.extra" #default>{{ card.extra }}</template>
          </MetricCard>
        </el-col>
        <!-- 磁盘合并为一张卡：全部真实挂载点的总用量；点击查看逐盘明细 -->
        <el-col :xs="12" :sm="12" :lg="6" class="dashboard__col">
          <div class="dashboard__clickable" @click="openDetail('disk')">
            <MetricCard
              title="磁盘"
              :value="formatBytes(diskSummary.used)"
              :unit="`/ ${formatBytes(diskSummary.total)}`"
              :percent="diskSummary.percent"
            >
              {{ diskSummary.count }} 个挂载点 · 点击查看
            </MetricCard>
          </div>
        </el-col>
        <el-col
          v-for="gpu in gpus"
          :key="`gpu-${gpu.index}`"
          :xs="12"
          :sm="12"
          :lg="6"
          class="dashboard__col"
        >
          <div class="dashboard__clickable" @click="openDetail('gpu')">
            <MetricCard
              :title="gpus.length > 1 ? `GPU${gpu.index} 利用率` : 'GPU 利用率'"
              :value="gpu.util_percent.toFixed(0)"
              unit="%"
              :percent="gpu.util_percent"
            >
              显存 {{ (gpu.mem_used_mb / 1024).toFixed(1) }}/{{
                (gpu.mem_total_mb / 1024).toFixed(1)
              }}
              GiB · {{ gpu.temp_c.toFixed(0) }}°C
            </MetricCard>
          </div>
        </el-col>
      </el-row>

      <div class="dashboard__range">
        <el-radio-group v-model="selectedMinutes" size="small">
          <el-radio-button v-for="opt in RANGE_OPTIONS" :key="opt.minutes" :value="opt.minutes">
            {{ opt.label }}
          </el-radio-button>
        </el-radio-group>
      </div>

      <el-row :gutter="16">
        <el-col :xs="24" :lg="hasGpu ? 6 : 8" class="dashboard__col">
          <el-card shadow="never" class="dashboard__chart-card" @click="openDetail('cpu')">
            <template #header>
              <div class="dashboard__chart-header">
                <span class="dashboard__chart-title">CPU 使用率</span>
                <span class="dashboard__chart-hint">点击查看详情</span>
              </div>
            </template>
            <TimeSeriesChart :series="cpuSeries" unit="%" :y-max="100" :height="220" area />
          </el-card>
        </el-col>
        <el-col :xs="24" :lg="hasGpu ? 6 : 8" class="dashboard__col">
          <el-card shadow="never" class="dashboard__chart-card" @click="openDetail('mem')">
            <template #header>
              <div class="dashboard__chart-header">
                <span class="dashboard__chart-title">内存 / Swap 占用</span>
                <span class="dashboard__chart-hint">点击查看详情</span>
              </div>
            </template>
            <TimeSeriesChart :series="memSeries" unit="%" :y-max="100" :height="220" />
          </el-card>
        </el-col>
        <el-col :xs="24" :lg="hasGpu ? 6 : 8" class="dashboard__col">
          <el-card shadow="never" class="dashboard__chart-card" @click="openDetail('net')">
            <template #header>
              <div class="dashboard__chart-header">
                <span class="dashboard__chart-title">网络速率（MiB/s）</span>
                <span class="dashboard__chart-hint">点击查看详情</span>
              </div>
            </template>
            <TimeSeriesChart :series="netSeries" unit=" MiB/s" :height="220" />
          </el-card>
        </el-col>
        <el-col v-if="hasGpu" :xs="24" :lg="6" class="dashboard__col">
          <el-card shadow="never" class="dashboard__chart-card" @click="openDetail('gpu')">
            <template #header>
              <div class="dashboard__chart-header">
                <span class="dashboard__chart-title">GPU 使用情况</span>
                <span class="dashboard__chart-hint">点击查看详情</span>
              </div>
            </template>
            <TimeSeriesChart :series="gpuSeries" unit="%" :y-max="100" :height="220" />
          </el-card>
        </el-col>
      </el-row>

      <!-- 图表详情：大图 + 细分曲线 + 当前值明细 -->
      <el-dialog
        :model-value="detailKey !== null"
        :title="detailKey ? DETAIL_TITLES[detailKey] : ''"
        width="80%"
        top="6vh"
        destroy-on-close
        @update:model-value="detailKey = null"
      >
        <template v-if="detailKey === 'cpu'">
          <div class="dashboard__detail-section">总使用率</div>
          <TimeSeriesChart :series="cpuSeries" unit="%" :y-max="100" :height="240" area />
          <div class="dashboard__detail-section">每核使用率</div>
          <TimeSeriesChart :series="perCpuSeries" unit="%" :y-max="100" :height="280" />
          <div class="dashboard__detail-section">平均负载</div>
          <TimeSeriesChart :series="loadSeries" :height="220" />
        </template>
        <template v-else-if="detailKey === 'mem'">
          <div class="dashboard__detail-section">占用百分比</div>
          <TimeSeriesChart :series="memSeries" unit="%" :y-max="100" :height="260" />
          <div class="dashboard__detail-section">绝对用量（GiB）</div>
          <TimeSeriesChart :series="memAbsSeries" unit=" GiB" :height="260" />
        </template>
        <template v-else-if="detailKey === 'net'">
          <div class="dashboard__detail-section">上下行速率（MiB/s）</div>
          <TimeSeriesChart :series="netSeries" unit=" MiB/s" :height="320" />
        </template>
        <template v-else-if="detailKey === 'gpu'">
          <div class="dashboard__detail-section">利用率 / 显存占用</div>
          <TimeSeriesChart :series="gpuSeries" unit="%" :y-max="100" :height="320" />
        </template>
        <template v-else-if="detailKey === 'disk'">
          <el-table :data="disks" size="small" empty-text="暂无磁盘数据">
            <el-table-column prop="mount" label="挂载点" min-width="160" show-overflow-tooltip />
            <el-table-column prop="device" label="设备" min-width="140" show-overflow-tooltip />
            <el-table-column prop="fstype" label="文件系统" width="100" />
            <el-table-column label="已用" width="110" align="right">
              <template #default="{ row }">{{ formatBytes(row.used) }}</template>
            </el-table-column>
            <el-table-column label="总量" width="110" align="right">
              <template #default="{ row }">{{ formatBytes(row.total) }}</template>
            </el-table-column>
            <el-table-column label="使用率" min-width="180">
              <template #default="{ row }">
                <el-progress
                  :percentage="Math.round(row.percent)"
                  :stroke-width="10"
                  :status="row.percent >= 90 ? 'exception' : row.percent >= 75 ? 'warning' : undefined"
                />
              </template>
            </el-table-column>
          </el-table>
        </template>
        <el-descriptions
          v-if="detailItems.length > 0"
          :column="2"
          border
          size="small"
          class="dashboard__detail-desc"
        >
          <el-descriptions-item v-for="item in detailItems" :key="item.label" :label="item.label">
            {{ item.value }}
          </el-descriptions-item>
        </el-descriptions>
      </el-dialog>

      <el-row :gutter="16">
        <el-col :xs="24" :lg="12" class="dashboard__col">
          <el-card shadow="never">
            <template #header>
              <span class="dashboard__chart-title">最近任务</span>
            </template>
            <el-table :data="overview?.recent_jobs ?? []" size="small" empty-text="暂无任务">
              <el-table-column prop="title" label="标题" min-width="160" show-overflow-tooltip />
              <el-table-column prop="type" label="类型" width="140" show-overflow-tooltip />
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag :type="jobStatusMeta(row.status).tagType" size="small">
                    {{ jobStatusMeta(row.status).text }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="创建时间" width="160">
                <template #default="{ row }">{{ formatMaybeTime(row.created_at) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
        <el-col :xs="24" :lg="12" class="dashboard__col">
          <el-card shadow="never">
            <template #header>
              <span class="dashboard__chart-title">安全事件</span>
            </template>
            <el-table
              :data="overview?.security_events ?? []"
              size="small"
              empty-text="暂无安全事件"
            >
              <el-table-column label="时间" width="160">
                <template #default="{ row }">{{ formatMaybeTime(row.ts) }}</template>
              </el-table-column>
              <el-table-column prop="action" label="动作" min-width="120" show-overflow-tooltip />
              <el-table-column prop="resource" label="资源" min-width="120" show-overflow-tooltip />
              <el-table-column label="结果" width="90">
                <template #default="{ row }">
                  <el-tag :type="resultTagType(row.result)" size="small">{{ row.result }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="source_ip" label="来源 IP" width="130" show-overflow-tooltip />
            </el-table>
          </el-card>
        </el-col>
      </el-row>

      <el-card shadow="never" class="dashboard__host">
        <template #header>
          <span class="dashboard__chart-title">主机信息</span>
        </template>
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item v-for="item in hostItems" :key="item.label" :label="item.label">
            {{ item.value }}
          </el-descriptions-item>
        </el-descriptions>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.dashboard__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.dashboard__chips {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.dashboard__col {
  margin-bottom: 16px;
}

.dashboard__range {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}

.dashboard__chart-title {
  font-size: 14px;
  font-weight: 600;
}

.dashboard__chart-card {
  cursor: pointer;
  transition: box-shadow 0.2s;
}

.dashboard__chart-card:hover {
  box-shadow: var(--el-box-shadow-light);
}

.dashboard__clickable {
  cursor: pointer;
}

.dashboard__clickable:hover {
  opacity: 0.85;
}

.dashboard__chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.dashboard__chart-hint {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

.dashboard__detail-section {
  margin: 12px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-regular);
}

.dashboard__detail-section:first-child {
  margin-top: 0;
}

.dashboard__detail-desc {
  margin-top: 16px;
}

.dashboard__host {
  margin-bottom: 16px;
}
</style>
