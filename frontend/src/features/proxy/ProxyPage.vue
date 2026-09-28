<script setup lang="ts">
// 代理管理（Clash/Mihomo）：核心服务开关、模式切换、流量（实时/历史）、代理组
// 节点选择与测速、规则（生效 + 自定义）、连接（活跃 + 最近关闭）、订阅（导入与
// 定期更新）、扩展配置/脚本、配置文件编辑。
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, ArrowRight, Download, Upload } from '@element-plus/icons-vue'
import { useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import { problemMessage } from '@/api/problem'
import {
  activateProfile,
  addCustomRule,
  closeConnection,
  closeConnections,
  createProfile,
  deleteCustomRule,
  deleteProfile,
  getActiveRules,
  getConfigFile,
  getConnections,
  getCustomRules,
  getExtend,
  getProfiles,
  getProxyGroups,
  getProxyService,
  getProxyStatus,
  getTrafficHistory,
  parseTraffic,
  patchMode,
  previewExtend,
  proxyServiceAction,
  proxyTrafficStreamUrl,
  putConfigFile,
  putExtend,
  refreshProfile,
  updateProfile as updateProfileApi,
  selectNode,
  testDelay,
  testGroupDelay,
} from '@/api/proxy'
import type {
  ActiveRule,
  CustomRule,
  ProfileCreatePayload,
  ProxyGroup,
  ProxyMode,
  ProxyNode,
  ProxyProfile,
  TrackedConnection,
} from '@/api/proxy'
import CodeEditor from '@/components/CodeEditor.vue'
import ReauthDialog from '@/components/ReauthDialog.vue'
import TimeSeriesChart from '@/components/TimeSeriesChart.vue'
import type { TimeSeriesSeriesData } from '@/components/TimeSeriesChart.vue'
import { formatDateTime } from '@/composables/format'
import { useResourceStream } from '@/composables/useResourceStream'

type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'

const activeTab = ref('overview')

// —— 核心服务开关（systemd 用户服务，与控制器可达性无关） ——

const {
  data: service,
  isPending: servicePending,
  refetch: refetchService,
} = useQuery({
  queryKey: ['proxy-service'],
  queryFn: getProxyService,
  refetchInterval: 15_000,
})

const serviceBusy = ref(false)
const serviceRunning = computed(() => service.value?.running ?? false)

/** systemd 状态词转中文展示。 */
const serviceStateText = computed(() => {
  const state = service.value?.state
  if (!state) {
    return '未知'
  }
  const map: Record<string, string> = {
    active: '运行中',
    activating: '启动中',
    inactive: '已停止',
    deactivating: '停止中',
    failed: '失败',
    unknown: '未知',
  }
  return map[state] ?? state
})

function onServiceSwitch(value: string | number | boolean): void {
  void switchService(Boolean(value))
}

async function switchService(next: boolean): Promise<void> {
  if (serviceBusy.value || next === serviceRunning.value) {
    return
  }
  if (!next) {
    const confirmed = await ElMessageBox.confirm(
      '停止核心服务后所有代理连接将中断，确定继续吗？',
      '停止代理服务',
      { type: 'warning', confirmButtonText: '停止', cancelButtonText: '取消' },
    ).then(
      () => true,
      () => false,
    )
    if (!confirmed) {
      return
    }
  }
  serviceBusy.value = true
  try {
    await proxyServiceAction(next ? 'start' : 'stop')
    ElMessage.success(next ? '核心服务已启动' : '核心服务已停止')
  } catch (error) {
    ElMessage.error(problemMessage(error, next ? '启动服务失败' : '停止服务失败'))
  } finally {
    serviceBusy.value = false
    await Promise.allSettled([refetchService(), refetchStatus()])
  }
}

async function handleServiceRestart(): Promise<void> {
  if (serviceBusy.value) {
    return
  }
  serviceBusy.value = true
  try {
    await proxyServiceAction('restart')
    ElMessage.success('核心服务已重启')
  } catch (error) {
    ElMessage.error(problemMessage(error, '重启服务失败'))
  } finally {
    serviceBusy.value = false
    await Promise.allSettled([refetchService(), refetchStatus()])
  }
}

// —— 状态与模式 ——

const {
  data: status,
  isPending: statusPending,
  isError: statusIsError,
  error: statusError,
  refetch: refetchStatus,
} = useQuery({ queryKey: ['proxy-status'], queryFn: getProxyStatus })

const statusMessage = computed(() => problemMessage(statusError.value, '获取代理状态失败'))

const MODES: { value: ProxyMode; label: string }[] = [
  { value: 'rule', label: '规则' },
  { value: 'global', label: '全局' },
  { value: 'direct', label: '直连' },
]

const mode = ref<ProxyMode>('rule')
const modeSwitching = ref(false)

watch(status, (value) => {
  if (value && (value.mode === 'rule' || value.mode === 'global' || value.mode === 'direct')) {
    mode.value = value.mode
  }
})

function onModeChange(value: string | number | boolean): void {
  void changeMode(value as ProxyMode)
}

async function changeMode(next: ProxyMode): Promise<void> {
  if (modeSwitching.value || next === mode.value) {
    return
  }
  const previous = mode.value
  mode.value = next
  modeSwitching.value = true
  try {
    await patchMode(next)
    ElMessage.success(`已切换为${MODES.find((m) => m.value === next)?.label ?? next}模式`)
    await refetchStatus()
  } catch (error) {
    mode.value = previous
    ElMessage.error(problemMessage(error, '切换模式失败'))
  } finally {
    modeSwitching.value = false
  }
}

// —— 流量：实时 SSE 滑窗 + 历史时间段（默认 24 小时） ——

const TRAFFIC_RANGES: { value: number; label: string }[] = [
  { value: 0, label: '实时' },
  { value: 60, label: '1 小时' },
  { value: 360, label: '6 小时' },
  { value: 1440, label: '24 小时' },
  { value: 10080, label: '7 天' },
]

/** 流量图窗口：0 = 实时滑窗，否则为分钟数（默认过去 24 小时）。 */
const trafficRange = ref(1440)

const MAX_POINTS = 60
const upSeries = ref<Array<[number, number]>>([])
const downSeries = ref<Array<[number, number]>>([])
const currentUp = ref(0)
const currentDown = ref(0)

const trafficHistoryEnabled = computed(() => trafficRange.value > 0)

const { data: trafficHistory } = useQuery({
  queryKey: ['proxy-traffic-history', trafficRange],
  queryFn: () => getTrafficHistory(trafficRange.value),
  enabled: trafficHistoryEnabled,
  refetchInterval: 60_000,
})

const trafficSeries = computed<TimeSeriesSeriesData[]>(() => {
  if (trafficRange.value === 0) {
    return [
      { name: '下载', data: downSeries.value },
      { name: '上传', data: upSeries.value },
    ]
  }
  const samples = trafficHistory.value?.samples ?? []
  return [
    {
      name: '下载',
      data: samples.map((s): [string, number] => [s.ts, Math.round((s.down / 1024) * 100) / 100]),
    },
    {
      name: '上传',
      data: samples.map((s): [string, number] => [s.ts, Math.round((s.up / 1024) * 100) / 100]),
    },
  ]
})

function pushTraffic(data: unknown): void {
  const traffic = parseTraffic(data)
  if (!traffic) {
    return
  }
  const now = Date.now()
  currentUp.value = traffic.up
  currentDown.value = traffic.down
  const upPoint: [number, number] = [now, traffic.up / 1024]
  const downPoint: [number, number] = [now, traffic.down / 1024]
  upSeries.value = [...upSeries.value, upPoint].slice(-MAX_POINTS)
  downSeries.value = [...downSeries.value, downPoint].slice(-MAX_POINTS)
}

const traffic = useResourceStream({
  topics: ['traffic'],
  onEvent: (_topic, data) => pushTraffic(data),
})

onMounted(() => {
  traffic.open(proxyTrafficStreamUrl())
})
// useResourceStream 随组件卸载自动关闭（tryOnScopeDispose），无需手动 close。

function formatRate(bytesPerSec: number): string {
  if (!Number.isFinite(bytesPerSec) || bytesPerSec < 0) {
    return '0 B/s'
  }
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s']
  let value = bytesPerSec
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index] ?? 'B/s'}`
}

// —— 代理组与测速 ——

const {
  data: groupsData,
  isPending: groupsPending,
  isError: groupsIsError,
  error: groupsError,
  refetch: refetchGroups,
} = useQuery({ queryKey: ['proxy-groups'], queryFn: getProxyGroups })

const groupsMessage = computed(() => problemMessage(groupsError.value, '获取代理组失败'))
const groups = computed(() => groupsData.value?.groups ?? [])

// 本地测速覆盖层：节点名 -> 延迟(ms)，优先于列表里的历史延迟。
const delays = reactive<Record<string, number>>({})
const testingGroup = reactive<Record<string, boolean>>({})
const testingNode = reactive<Record<string, boolean>>({})
const selectingGroup = reactive<Record<string, boolean>>({})

// —— 分组折叠状态（localStorage 持久化） ——

const COLLAPSED_KEY = 'proxy.groups.collapsed'

/** 读取已折叠的组名；存储不可用（无痕模式）或数据损坏时当作全部展开。 */
function readCollapsed(): string[] {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.filter((n): n is string => typeof n === 'string') : []
  } catch {
    return []
  }
}

const collapsedGroups = ref<Set<string>>(new Set(readCollapsed()))

function isCollapsed(group: ProxyGroup): boolean {
  return collapsedGroups.value.has(group.name)
}

function toggleGroup(group: ProxyGroup): void {
  const next = new Set(collapsedGroups.value)
  if (next.has(group.name)) {
    next.delete(group.name)
  } else {
    next.add(group.name)
  }
  collapsedGroups.value = next
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...next]))
  } catch {
    // 存储不可用时只影响“下次记住”，本次折叠仍然生效。
  }
}

function nodeDelay(node: ProxyNode): number {
  return delays[node.name] ?? node.delay
}

function delayTagType(delay: number): TagType {
  if (delay <= 0) {
    return 'info'
  }
  if (delay < 200) {
    return 'success'
  }
  if (delay < 500) {
    return 'warning'
  }
  return 'danger'
}

function delayText(delay: number): string {
  return delay > 0 ? `${delay} ms` : '—'
}

// 成员本身是代理组时，组自身没有延迟记录，显示的是它当前节点的延迟。
function delayHint(node: ProxyNode): string {
  const delay = nodeDelay(node)
  if (testingNode[node.name]) {
    return `正在测速：${node.name}`
  }
  if (delay <= 0) {
    return '未测速或已超时（点击重新测速）'
  }
  if (delays[node.name] !== undefined || !node.delay_of) {
    return `${node.name}：${delay} ms（点击重新测速）`
  }
  return `继承自当前节点 ${node.delay_of}：${delay} ms（点击重新测速）`
}

/** 单独复测一个节点：大组里只想确认某个节点时不必重测整组。 */
async function handleNodeTest(node: ProxyNode): Promise<void> {
  if (testingNode[node.name]) {
    return
  }
  testingNode[node.name] = true
  try {
    const { delay } = await testDelay(node.name)
    // 超时返回 delay=0，同样写入覆盖层，避免继续显示过期的历史延迟。
    delays[node.name] = delay
  } catch (error) {
    ElMessage.error(problemMessage(error, '测速失败'))
  } finally {
    testingNode[node.name] = false
  }
}

function isSelectable(group: ProxyGroup): boolean {
  return group.type === 'Selector'
}

async function handleSelect(group: ProxyGroup, node: ProxyNode): Promise<void> {
  if (!isSelectable(group) || selectingGroup[group.name] || node.name === group.now) {
    return
  }
  selectingGroup[group.name] = true
  try {
    await selectNode(group.name, node.name)
    ElMessage.success(`已切换 ${group.name} → ${node.name}`)
    await refetchGroups()
  } catch (error) {
    ElMessage.error(problemMessage(error, '切换节点失败'))
  } finally {
    selectingGroup[group.name] = false
  }
}

async function handleTest(group: ProxyGroup): Promise<void> {
  if (testingGroup[group.name]) {
    return
  }
  testingGroup[group.name] = true
  try {
    // 整组一次请求：大组（GLOBAL 可达上百个节点）逐个测速会瞬间打出上百个并发请求。
    const { delays: measured } = await testGroupDelay(group.name)
    for (const node of group.all) {
      delays[node.name] = measured[node.name] ?? 0
    }
  } catch (error) {
    ElMessage.error(problemMessage(error, '测速失败'))
  } finally {
    testingGroup[group.name] = false
  }
}

// —— 规则：生效规则（搜索 + 分页）与自定义规则（增删，不被配置上传覆盖） ——

const rulesTabActive = computed(() => activeTab.value === 'rules')

const {
  data: activeRulesData,
  isPending: activeRulesPending,
  isError: activeRulesIsError,
  error: activeRulesError,
  refetch: refetchActiveRules,
} = useQuery({
  queryKey: ['proxy-rules'],
  queryFn: getActiveRules,
  enabled: rulesTabActive,
})

const activeRulesMessage = computed(() => problemMessage(activeRulesError.value, '获取规则失败'))

const {
  data: customRulesData,
  isPending: customRulesPending,
  refetch: refetchCustomRules,
} = useQuery({
  queryKey: ['proxy-custom-rules'],
  queryFn: getCustomRules,
  enabled: rulesTabActive,
})

const customRules = computed(() => customRulesData.value?.rules ?? [])

const RULE_PAGE_SIZE = 50
const ruleSearch = ref('')
const rulePage = ref(1)

const filteredRules = computed<ActiveRule[]>(() => {
  const all = activeRulesData.value?.rules ?? []
  const query = ruleSearch.value.trim().toLowerCase()
  if (!query) {
    return all
  }
  return all.filter(
    (r) =>
      r.type.toLowerCase().includes(query) ||
      r.payload.toLowerCase().includes(query) ||
      r.proxy.toLowerCase().includes(query),
  )
})

const pagedRules = computed(() => {
  const start = (rulePage.value - 1) * RULE_PAGE_SIZE
  return filteredRules.value.slice(start, start + RULE_PAGE_SIZE)
})

watch(ruleSearch, () => {
  rulePage.value = 1
})

/** 常用规则类型（下拉可手输其它类型）。 */
const RULE_TYPES = [
  'DOMAIN-SUFFIX',
  'DOMAIN',
  'DOMAIN-KEYWORD',
  'DOMAIN-REGEX',
  'GEOSITE',
  'GEOIP',
  'IP-CIDR',
  'IP-CIDR6',
  'SRC-IP-CIDR',
  'SRC-PORT',
  'DST-PORT',
  'PROCESS-NAME',
  'MATCH',
]

const newRuleType = ref('DOMAIN-SUFFIX')
const newRuleValue = ref('')
const newRulePolicy = ref('DIRECT')
const addingRule = ref(false)

/** 策略候选：内置出口 + 全部代理组名。 */
const policyOptions = computed(() => [
  'DIRECT',
  'REJECT',
  ...groups.value.map((g) => g.name),
])

const newRulePreview = computed(() => {
  if (newRuleType.value === 'MATCH') {
    return `MATCH,${newRulePolicy.value}`
  }
  const value = newRuleValue.value.trim()
  return value ? `${newRuleType.value},${value},${newRulePolicy.value}` : ''
})

async function handleAddRule(): Promise<void> {
  const rule = newRulePreview.value
  if (!rule) {
    ElMessage.warning('请填写匹配内容')
    return
  }
  addingRule.value = true
  try {
    await addCustomRule(rule)
    ElMessage.success('规则已添加并生效')
    newRuleValue.value = ''
    await Promise.allSettled([refetchCustomRules(), refetchActiveRules()])
  } catch (error) {
    ElMessage.error(problemMessage(error, '添加规则失败'))
  } finally {
    addingRule.value = false
  }
}

const deletingRuleId = ref<string | null>(null)

async function handleDeleteRule(row: CustomRule): Promise<void> {
  const confirmed = await ElMessageBox.confirm(
    `确定删除规则「${row.rule}」吗？删除后立即从配置中移除。`,
    '删除自定义规则',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  ).then(
    () => true,
    () => false,
  )
  if (!confirmed) {
    return
  }
  deletingRuleId.value = row.id
  try {
    await deleteCustomRule(row.id)
    ElMessage.success('规则已删除')
    await Promise.allSettled([refetchCustomRules(), refetchActiveRules()])
  } catch (error) {
    ElMessage.error(problemMessage(error, '删除规则失败'))
  } finally {
    deletingRuleId.value = null
  }
}

// —— 配置文件（懒加载编辑器 + 保存重载，写入需重认证） ——

const configLoaded = ref(false)
const configLoading = ref(false)
const configSaving = ref(false)
const configEditing = ref(false)
const configPath = ref('')
const reauthVisible = ref(false)
/** 重认证通过后要重试的动作：配置文件保存或扩展保存。 */
const reauthRetry = ref<(() => Promise<void>) | null>(null)
const configContent = ref('')
const configEditor = ref<InstanceType<typeof CodeEditor> | null>(null)

async function ensureConfigEditor(): Promise<void> {
  if (configLoaded.value || configLoading.value) {
    return
  }
  configLoading.value = true
  try {
    const res = await getConfigFile()
    configPath.value = res.path
    configContent.value = res.content
    configLoaded.value = true
  } catch (error) {
    ElMessage.error(problemMessage(error, '读取配置文件失败'))
  } finally {
    configLoading.value = false
  }
}

watch(activeTab, (name) => {
  if (name === 'config') {
    void ensureConfigEditor()
  }
})

function startEditing(): void {
  configEditing.value = true
  configEditor.value?.focus()
}

async function reloadConfig(): Promise<void> {
  configLoading.value = true
  try {
    const res = await getConfigFile()
    configPath.value = res.path
    configContent.value = res.content
    configEditing.value = false
  } catch (error) {
    ElMessage.error(problemMessage(error, '重新加载失败'))
  } finally {
    configLoading.value = false
  }
}

async function saveConfig(): Promise<void> {
  if (configSaving.value) {
    return
  }
  configSaving.value = true
  try {
    await putConfigFile(configContent.value)
    ElMessage.success('已保存并重载配置')
    configEditing.value = false
  } catch (error) {
    if ((error as ApiError | undefined)?.problem?.code === 'auth.reauth_required') {
      // 触发重认证对话框；验证通过后自动重试保存。
      reauthRetry.value = saveConfig
      reauthVisible.value = true
    } else {
      ElMessage.error(problemMessage(error, '保存失败'))
    }
  } finally {
    configSaving.value = false
  }
}

function onReauthSuccess(): void {
  void reauthRetry.value?.()
}

// —— 订阅：多订阅源，选一个为当前生效；可设定期自动更新 ——

const profilesTabActive = computed(() => activeTab.value === 'profiles')
// 扩展页也要用订阅列表（预览需要一个基准订阅），故与订阅页共用同一次查询。
const extendTabActive = computed(() => activeTab.value === 'extend')

const {
  data: profilesData,
  isPending: profilesPending,
  refetch: refetchProfiles,
} = useQuery({
  queryKey: ['proxy-profiles'],
  queryFn: getProfiles,
  enabled: computed(() => profilesTabActive.value || extendTabActive.value),
})

const profiles = computed(() => profilesData.value?.profiles ?? [])

/** 更新间隔候选（分钟）；0 表示不自动更新，后端下限为 5 分钟。 */
const INTERVAL_OPTIONS = [
  { label: '不自动更新', value: 0 },
  { label: '每 30 分钟', value: 30 },
  { label: '每 1 小时', value: 60 },
  { label: '每 6 小时', value: 360 },
  { label: '每 12 小时', value: 720 },
  { label: '每天', value: 1440 },
]

function intervalText(minutes: number): string {
  if (minutes <= 0) {
    return '手动'
  }
  const known = INTERVAL_OPTIONS.find((option) => option.value === minutes)
  return known ? known.label : `每 ${minutes} 分钟`
}

/** 订阅用量：已用 / 总量，附到期日；机场没返回用量时为空。 */
function usageText(profile: ProxyProfile): string {
  const info = profile.subscription
  const used = (info.upload ?? 0) + (info.download ?? 0)
  const parts: string[] = []
  if (info.total) {
    parts.push(`${formatBytes(used)} / ${formatBytes(info.total)}`)
  } else if (used) {
    parts.push(formatBytes(used))
  }
  if (info.expire) {
    parts.push(`到期 ${formatDateTime(new Date(info.expire * 1000).toISOString())}`)
  }
  return parts.join(' · ')
}

const profileDialogVisible = ref(false)
const profileSaving = ref(false)
/** 非空表示编辑该 id，空串表示新增。 */
const profileEditingId = ref('')
const profileForm = reactive<ProfileCreatePayload>({
  name: '',
  url: '',
  interval_min: 0,
  user_agent: '',
})

function openProfileDialog(profile?: ProxyProfile): void {
  profileEditingId.value = profile?.id ?? ''
  profileForm.name = profile?.name ?? ''
  profileForm.url = profile?.url ?? ''
  profileForm.interval_min = profile?.interval_min ?? 0
  profileForm.user_agent = profile?.user_agent ?? ''
  profileDialogVisible.value = true
}

async function submitProfile(): Promise<void> {
  if (!profileForm.url.trim()) {
    ElMessage.warning('请填写订阅地址')
    return
  }
  profileSaving.value = true
  try {
    if (profileEditingId.value) {
      await updateProfileApi(profileEditingId.value, { ...profileForm })
      ElMessage.success('订阅已更新')
    } else {
      // 新增时后端会立即下载；首个订阅自动成为当前生效并重新生成配置。
      await createProfile({ ...profileForm })
      ElMessage.success('订阅已添加并下载')
    }
    profileDialogVisible.value = false
    await refetchProfiles()
  } catch (error) {
    ElMessage.error(problemMessage(error, '保存订阅失败'))
  } finally {
    profileSaving.value = false
  }
}

const profileBusyId = ref('')

async function withProfileBusy(id: string, action: () => Promise<void>): Promise<void> {
  if (profileBusyId.value) {
    return
  }
  profileBusyId.value = id
  try {
    await action()
  } finally {
    profileBusyId.value = ''
    await refetchProfiles()
  }
}

async function handleRefreshProfile(profile: ProxyProfile): Promise<void> {
  await withProfileBusy(profile.id, async () => {
    try {
      await refreshProfile(profile.id)
      ElMessage.success(profile.active ? '已更新并重新生成配置' : '已更新')
    } catch (error) {
      ElMessage.error(problemMessage(error, '更新订阅失败'))
    }
  })
}

async function handleActivateProfile(profile: ProxyProfile): Promise<void> {
  await withProfileBusy(profile.id, async () => {
    try {
      await activateProfile(profile.id)
      ElMessage.success(`已切换到「${profile.name}」并重新生成配置`)
      // 生成的配置换了，配置文件页需要重新拉。
      configLoaded.value = false
      await refetchGroups()
    } catch (error) {
      ElMessage.error(problemMessage(error, '切换订阅失败'))
    }
  })
}

async function handleDeleteProfile(profile: ProxyProfile): Promise<void> {
  const confirmed = await ElMessageBox.confirm(
    `删除订阅「${profile.name}」？已生成的配置文件不会改动。`,
    '删除订阅',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  ).then(
    () => true,
    () => false,
  )
  if (!confirmed) {
    return
  }
  await withProfileBusy(profile.id, async () => {
    try {
      await deleteProfile(profile.id)
      ElMessage.success('订阅已删除')
    } catch (error) {
      ElMessage.error(problemMessage(error, '删除订阅失败'))
    }
  })
}

// —— 扩展配置（merge YAML）与扩展脚本（JavaScript 沙箱） ——

const extendMerge = ref('')
const extendScript = ref('')
const extendLoading = ref(false)
const extendLoaded = ref(false)
const extendSaving = ref(false)
const extendPreviewing = ref(false)
const extendPreview = ref('')
const extendLogs = ref<string[]>([])

async function ensureExtend(): Promise<void> {
  if (extendLoaded.value || extendLoading.value) {
    return
  }
  extendLoading.value = true
  try {
    const res = await getExtend()
    extendMerge.value = res.merge
    extendScript.value = res.script
    extendLoaded.value = true
  } catch (error) {
    ElMessage.error(problemMessage(error, '读取扩展配置失败'))
  } finally {
    extendLoading.value = false
  }
}

watch(activeTab, (name) => {
  if (name === 'extend') {
    void ensureExtend()
  }
})

async function handlePreviewExtend(): Promise<void> {
  if (extendPreviewing.value) {
    return
  }
  extendPreviewing.value = true
  try {
    const res = await previewExtend({ merge: extendMerge.value, script: extendScript.value })
    extendPreview.value = res.content
    extendLogs.value = res.logs
    ElMessage.success('预览已生成（未写入磁盘）')
  } catch (error) {
    extendPreview.value = ''
    extendLogs.value = []
    ElMessage.error(problemMessage(error, '预览失败'))
  } finally {
    extendPreviewing.value = false
  }
}

async function saveExtend(): Promise<void> {
  if (extendSaving.value) {
    return
  }
  extendSaving.value = true
  try {
    await putExtend({ merge: extendMerge.value, script: extendScript.value })
    ElMessage.success('已保存并重新生成配置')
    // 预览面板标着「未写入磁盘」，保存后这句话不再成立，留着会误导。
    extendPreview.value = ''
    extendLogs.value = []
    // 配置文件是流水线的产物，重新生成后缓存作废。
    configLoaded.value = false
    await refetchGroups()
  } catch (error) {
    if ((error as ApiError | undefined)?.problem?.code === 'auth.reauth_required') {
      reauthRetry.value = saveExtend
      reauthVisible.value = true
    } else {
      ElMessage.error(problemMessage(error, '保存失败'))
    }
  } finally {
    extendSaving.value = false
  }
}

// —— 连接：活跃（tracker 快照，3s 轮询）+ 最近 100 条已关闭 ——

const connectionsActive = computed(() => activeTab.value === 'connections')

const {
  data: connectionsData,
  isPending: connectionsPending,
  isError: connectionsIsError,
  error: connectionsError,
  refetch: refetchConnections,
} = useQuery({
  queryKey: ['proxy-connections'],
  queryFn: getConnections,
  enabled: connectionsActive,
  refetchInterval: 3000,
})

const connectionsMessage = computed(() =>
  problemMessage(connectionsError.value, '获取连接列表失败'),
)

/** 活跃连接：新建立的在前。 */
const activeConnections = computed<TrackedConnection[]>(() => {
  const list = connectionsData.value?.active ?? []
  return [...list].sort((a, b) => (a.start < b.start ? 1 : -1))
})

const closedConnections = computed<TrackedConnection[]>(() => connectionsData.value?.closed ?? [])

const controllerUnavailable = computed(
  () => connectionsData.value !== undefined && !connectionsData.value.available,
)

const connectionSummary = computed(() => ({
  count: activeConnections.value.length,
  download: connectionsData.value?.download_total ?? 0,
  upload: connectionsData.value?.upload_total ?? 0,
}))

/** 链路展示：反转 chains（入口组 → 出口节点）。 */
function chainText(conn: TrackedConnection): string {
  if (conn.chains.length === 0) {
    return '—'
  }
  return [...conn.chains].reverse().join(' → ')
}

/** 规则展示：规则类型 + 匹配内容。 */
function ruleText(conn: TrackedConnection): string {
  if (!conn.rule) {
    return '—'
  }
  return conn.rule_payload ? `${conn.rule}(${conn.rule_payload})` : conn.rule
}

const closingConnId = ref<string | null>(null)

async function handleCloseConnection(conn: TrackedConnection): Promise<void> {
  closingConnId.value = conn.id
  try {
    await closeConnection(conn.id)
    ElMessage.success(`已关闭连接 ${conn.host}`)
    await refetchConnections()
  } catch (error) {
    ElMessage.error(problemMessage(error, '关闭连接失败'))
  } finally {
    closingConnId.value = null
  }
}

const clearingConnections = ref(false)

async function handleClearConnections(): Promise<void> {
  const confirmed = await ElMessageBox.confirm('确定要关闭全部连接吗？', '清空连接', {
    type: 'warning',
    confirmButtonText: '清空',
    cancelButtonText: '取消',
  }).then(
    () => true,
    () => false,
  )
  if (!confirmed) {
    return
  }
  clearingConnections.value = true
  try {
    await closeConnections()
    ElMessage.success('已清空全部连接')
    await refetchConnections()
  } catch (error) {
    ElMessage.error(problemMessage(error, '清空连接失败'))
  } finally {
    clearingConnections.value = false
  }
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return '0 B'
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index] ?? 'B'}`
}
</script>

<template>
  <div class="proxy-page">
    <el-tabs v-model="activeTab" class="proxy-page__tabs">
      <!-- 概览：服务开关 + 状态 + 流量 + 代理组 -->
      <el-tab-pane label="概览" name="overview">
        <el-row :gutter="16" class="proxy-page__row">
          <el-col :xs="24" :lg="12">
            <el-card shadow="never" class="proxy-page__card">
              <template #header>
                <div class="proxy-page__card-header">
                  <span class="proxy-page__card-title">控制器状态</span>
                  <el-button
                    link
                    type="primary"
                    :loading="statusPending"
                    @click="() => refetchStatus()"
                  >
                    刷新
                  </el-button>
                </div>
              </template>
              <!-- 核心服务开关独立于控制器状态：控制器不可达时仍可开机 -->
              <div class="proxy-page__service">
                <span class="proxy-page__label">核心服务</span>
                <el-tag
                  size="small"
                  :type="serviceRunning ? 'success' : 'info'"
                  effect="plain"
                >
                  {{ serviceStateText }}
                </el-tag>
                <el-switch
                  :model-value="serviceRunning"
                  :loading="serviceBusy"
                  :disabled="servicePending || serviceBusy"
                  @change="onServiceSwitch"
                />
                <el-button
                  link
                  type="primary"
                  :disabled="!serviceRunning || serviceBusy"
                  @click="handleServiceRestart"
                >
                  重启
                </el-button>
              </div>
              <el-skeleton v-if="statusPending" :rows="4" animated />
              <el-alert
                v-else-if="statusIsError"
                type="error"
                :title="statusMessage"
                :closable="false"
                show-icon
              />
              <template v-else-if="status">
                <div class="proxy-page__mode">
                  <span class="proxy-page__label">代理模式</span>
                  <el-radio-group
                    :model-value="mode"
                    :disabled="modeSwitching"
                    @change="onModeChange"
                  >
                    <el-radio-button v-for="m in MODES" :key="m.value" :value="m.value">
                      {{ m.label }}
                    </el-radio-button>
                  </el-radio-group>
                </div>
                <el-descriptions :column="2" border class="proxy-page__desc">
                  <el-descriptions-item label="版本">
                    {{ status.version || '—' }}
                  </el-descriptions-item>
                  <el-descriptions-item label="混合端口">
                    {{ status.mixed_port || '—' }}
                  </el-descriptions-item>
                  <el-descriptions-item label="允许局域网">
                    {{ status.allow_lan ? '是' : '否' }}
                  </el-descriptions-item>
                  <el-descriptions-item label="日志级别">
                    {{ status.log_level || '—' }}
                  </el-descriptions-item>
                </el-descriptions>
              </template>
            </el-card>
          </el-col>

          <el-col :xs="24" :lg="12">
            <el-card shadow="never" class="proxy-page__card">
              <template #header>
                <div class="proxy-page__card-header">
                  <span class="proxy-page__card-title">流量</span>
                  <div class="proxy-page__traffic-controls">
                    <div class="proxy-page__rates">
                      <span class="proxy-page__rate">
                        <el-icon><Download /></el-icon>{{ formatRate(currentDown) }}
                      </span>
                      <span class="proxy-page__rate">
                        <el-icon><Upload /></el-icon>{{ formatRate(currentUp) }}
                      </span>
                    </div>
                    <el-radio-group v-model="trafficRange" size="small">
                      <el-radio-button
                        v-for="range in TRAFFIC_RANGES"
                        :key="range.value"
                        :value="range.value"
                      >
                        {{ range.label }}
                      </el-radio-button>
                    </el-radio-group>
                  </div>
                </div>
              </template>
              <TimeSeriesChart :series="trafficSeries" :height="220" unit=" KB/s" />
            </el-card>
          </el-col>
        </el-row>

        <el-card shadow="never" class="proxy-page__card">
          <template #header>
            <div class="proxy-page__card-header">
              <span class="proxy-page__card-title">代理组</span>
              <el-button
                link
                type="primary"
                :loading="groupsPending"
                @click="() => refetchGroups()"
              >
                刷新
              </el-button>
            </div>
          </template>
          <el-skeleton v-if="groupsPending" :rows="5" animated />
          <el-alert
            v-else-if="groupsIsError"
            type="error"
            :title="groupsMessage"
            :closable="false"
            show-icon
          />
          <el-empty v-else-if="groups.length === 0" description="没有可选择的代理组" />
          <template v-else>
            <div v-for="group in groups" :key="group.name" class="proxy-page__group">
              <div class="proxy-page__group-header">
                <div
                  class="proxy-page__group-title"
                  role="button"
                  tabindex="0"
                  :aria-expanded="!isCollapsed(group)"
                  @click="toggleGroup(group)"
                  @keydown.enter.prevent="toggleGroup(group)"
                  @keydown.space.prevent="toggleGroup(group)"
                >
                  <el-icon class="proxy-page__group-caret">
                    <ArrowRight v-if="isCollapsed(group)" />
                    <ArrowDown v-else />
                  </el-icon>
                  <span class="proxy-page__group-name">{{ group.name }}</span>
                  <el-tag size="small" type="info" effect="plain">{{ group.type }}</el-tag>
                  <span class="proxy-page__group-now">当前：{{ group.now || '—' }}</span>
                  <span v-if="isCollapsed(group)" class="proxy-page__group-count">
                    {{ group.all.length }} 个节点
                  </span>
                </div>
                <el-button
                  link
                  type="primary"
                  :loading="testingGroup[group.name] ?? false"
                  @click="handleTest(group)"
                >
                  测速
                </el-button>
              </div>
              <div v-show="!isCollapsed(group)" class="proxy-page__nodes">
                <div
                  v-for="node in group.all"
                  :key="node.name"
                  class="proxy-page__node"
                  :class="{
                    'is-active': node.name === group.now,
                    'is-selectable': isSelectable(group),
                  }"
                  @click="handleSelect(group, node)"
                >
                  <span class="proxy-page__node-name">{{ node.name }}</span>
                  <el-tooltip :content="delayHint(node)" placement="top" :show-after="300">
                    <!-- 包一层原生元素：el-tag 的 click 是组件事件，.stop 拦不住冒泡到节点选择 -->
                    <span class="proxy-page__node-delay" @click.stop="handleNodeTest(node)">
                      <el-tag
                        size="small"
                        :type="delayTagType(nodeDelay(node))"
                        :effect="
                          node.delay_of && delays[node.name] === undefined ? 'plain' : 'light'
                        "
                      >
                        {{ testingNode[node.name] ? '…' : delayText(nodeDelay(node)) }}
                      </el-tag>
                    </span>
                  </el-tooltip>
                </div>
              </div>
            </div>
          </template>
        </el-card>
      </el-tab-pane>

      <!-- 规则：自定义规则管理 + 当前生效规则 -->
      <el-tab-pane label="规则" name="rules">
        <el-card shadow="never" class="proxy-page__card">
          <template #header>
            <div class="proxy-page__card-header">
              <span class="proxy-page__card-title">自定义规则</span>
              <span class="proxy-page__hint">
                保存在面板中并注入配置文件开头，上传新配置不会覆盖
              </span>
            </div>
          </template>
          <div class="proxy-page__rule-form">
            <el-select
              v-model="newRuleType"
              filterable
              allow-create
              class="proxy-page__rule-type"
            >
              <el-option v-for="t in RULE_TYPES" :key="t" :label="t" :value="t" />
            </el-select>
            <el-input
              v-model="newRuleValue"
              :disabled="newRuleType === 'MATCH'"
              :placeholder="newRuleType === 'MATCH' ? 'MATCH 无需匹配内容' : '匹配内容，如 example.com'"
              class="proxy-page__rule-value"
              @keyup.enter="handleAddRule"
            />
            <el-select
              v-model="newRulePolicy"
              filterable
              allow-create
              class="proxy-page__rule-policy"
            >
              <el-option v-for="p in policyOptions" :key="p" :label="p" :value="p" />
            </el-select>
            <el-button type="primary" :loading="addingRule" @click="handleAddRule">
              添加
            </el-button>
          </div>
          <div v-if="newRulePreview" class="proxy-page__rule-preview">
            预览：<code>{{ newRulePreview }}</code>
          </div>
          <el-skeleton v-if="customRulesPending && rulesTabActive" :rows="2" animated />
          <el-empty
            v-else-if="customRules.length === 0"
            description="暂无自定义规则"
            :image-size="60"
          />
          <el-table v-else :data="customRules" size="small">
            <el-table-column label="规则" min-width="320">
              <template #default="{ row }">
                <code class="proxy-page__rule-code">{{ row.rule }}</code>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="180">
              <template #default="{ row }">
                {{ row.created_at ? formatDateTime(row.created_at) : '—' }}
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90" align="right">
              <template #default="{ row }">
                <el-button
                  link
                  type="danger"
                  :loading="deletingRuleId === row.id"
                  @click="handleDeleteRule(row)"
                >
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never" class="proxy-page__card">
          <template #header>
            <div class="proxy-page__card-header">
              <div class="proxy-page__card-title-group">
                <span class="proxy-page__card-title">当前生效规则</span>
                <el-tag size="small" type="info" effect="plain">
                  {{ filteredRules.length }} 条
                </el-tag>
              </div>
              <div class="proxy-page__rule-toolbar">
                <el-input
                  v-model="ruleSearch"
                  placeholder="搜索类型 / 内容 / 策略"
                  clearable
                  class="proxy-page__rule-search"
                />
                <el-button
                  link
                  type="primary"
                  :loading="activeRulesPending"
                  @click="() => refetchActiveRules()"
                >
                  刷新
                </el-button>
              </div>
            </div>
          </template>
          <el-skeleton v-if="activeRulesPending" :rows="6" animated />
          <el-alert
            v-else-if="activeRulesIsError"
            type="error"
            :title="activeRulesMessage"
            :closable="false"
            show-icon
          />
          <template v-else>
            <el-table :data="pagedRules" size="small">
              <template #empty>
                <el-empty description="没有匹配的规则" :image-size="60" />
              </template>
              <el-table-column label="#" width="70">
                <template #default="{ $index }">
                  {{ (rulePage - 1) * RULE_PAGE_SIZE + $index + 1 }}
                </template>
              </el-table-column>
              <el-table-column prop="type" label="类型" width="150" show-overflow-tooltip />
              <el-table-column prop="payload" label="内容" min-width="220" show-overflow-tooltip />
              <el-table-column prop="proxy" label="策略" min-width="140" show-overflow-tooltip />
            </el-table>
            <div v-if="filteredRules.length > RULE_PAGE_SIZE" class="proxy-page__rule-pager">
              <el-pagination
                v-model:current-page="rulePage"
                layout="prev, pager, next"
                :page-size="RULE_PAGE_SIZE"
                :total="filteredRules.length"
              />
            </div>
          </template>
        </el-card>
      </el-tab-pane>

      <!-- 订阅：多订阅源，选一个为当前生效；可设定期自动更新 -->
      <el-tab-pane label="订阅" name="profiles">
        <el-card shadow="never" class="proxy-page__card">
          <template #header>
            <div class="proxy-page__card-header">
              <span class="proxy-page__card-title">订阅源</span>
              <div>
                <el-button link type="primary" :loading="profilesPending" @click="() => refetchProfiles()">
                  刷新
                </el-button>
                <el-button type="primary" @click="openProfileDialog()">添加订阅</el-button>
              </div>
            </div>
          </template>
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="配置文件由「当前生效的订阅 → 扩展配置 → 扩展脚本」自动生成；订阅更新不会覆盖你在扩展里做的定制"
            class="proxy-page__config-alert"
          />
          <el-skeleton v-if="profilesPending" :rows="3" animated />
          <el-empty v-else-if="profiles.length === 0" description="还没有订阅，点击右上角添加" />
          <el-table v-else :data="profiles" size="small">
            <el-table-column label="名称" min-width="150">
              <template #default="{ row }">
                <span>{{ row.name }}</span>
                <el-tag v-if="row.active" size="small" type="success" class="proxy-page__profile-tag">
                  当前生效
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="地址" min-width="240" show-overflow-tooltip>
              <template #default="{ row }">{{ row.url }}</template>
            </el-table-column>
            <el-table-column label="自动更新" width="110">
              <template #default="{ row }">{{ intervalText(row.interval_min) }}</template>
            </el-table-column>
            <el-table-column label="上次更新" width="170">
              <template #default="{ row }">
                {{ row.last_updated ? formatDateTime(row.last_updated) : '—' }}
              </template>
            </el-table-column>
            <el-table-column label="用量" min-width="200">
              <template #default="{ row }">
                <span v-if="usageText(row)">{{ usageText(row) }}</span>
                <el-tooltip v-else-if="row.last_error" :content="row.last_error" placement="top">
                  <el-tag size="small" type="danger">更新失败</el-tag>
                </el-tooltip>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="230" fixed="right" align="right">
              <template #default="{ row }">
                <el-button
                  link
                  type="primary"
                  :loading="profileBusyId === row.id"
                  @click="handleRefreshProfile(row)"
                >
                  更新
                </el-button>
                <el-button
                  v-if="!row.active"
                  link
                  type="primary"
                  :disabled="profileBusyId !== ''"
                  @click="handleActivateProfile(row)"
                >
                  设为当前
                </el-button>
                <el-button link type="primary" @click="openProfileDialog(row)">编辑</el-button>
                <el-button
                  link
                  type="danger"
                  :disabled="profileBusyId !== ''"
                  @click="handleDeleteProfile(row)"
                >
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-dialog
          v-model="profileDialogVisible"
          :title="profileEditingId ? '编辑订阅' : '添加订阅'"
          width="560px"
        >
          <el-form label-width="110px" @submit.prevent>
            <el-form-item label="名称">
              <el-input v-model="profileForm.name" placeholder="留空则自动命名" maxlength="128" />
            </el-form-item>
            <el-form-item label="订阅地址" required>
              <el-input v-model="profileForm.url" placeholder="https://..." maxlength="2048" />
            </el-form-item>
            <el-form-item label="自动更新">
              <el-select v-model="profileForm.interval_min" class="proxy-page__profile-select">
                <el-option
                  v-for="option in INTERVAL_OPTIONS"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="User-Agent">
              <el-input
                v-model="profileForm.user_agent"
                placeholder="留空使用默认（多数机场按 UA 返回 Clash 格式）"
                maxlength="200"
              />
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="profileDialogVisible = false">取消</el-button>
            <el-button type="primary" :loading="profileSaving" @click="submitProfile">
              {{ profileEditingId ? '保存' : '添加并下载' }}
            </el-button>
          </template>
        </el-dialog>
      </el-tab-pane>

      <!-- 扩展：merge YAML + JavaScript 脚本，作用于订阅内容之上 -->
      <el-tab-pane label="扩展" name="extend">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="扩展配置与扩展脚本会在每次订阅更新后重新应用，因此本地定制不会被覆盖。保存前建议先预览。"
          class="proxy-page__config-alert"
        />
        <el-card shadow="never" class="proxy-page__card">
          <template #header>
            <div class="proxy-page__card-header">
              <span class="proxy-page__card-title">扩展配置（YAML）</span>
              <span class="proxy-page__hint">
                支持 prepend-rules / append-rules / prepend-proxies / append-proxies /
                prepend-proxy-groups / append-proxy-groups，其余键与订阅深度合并
              </span>
            </div>
          </template>
          <CodeEditor
            v-model="extendMerge"
            language="yaml"
            height="260px"
            :fallback-rows="12"
            :loading="extendLoading"
          />
        </el-card>

        <el-card shadow="never" class="proxy-page__card">
          <template #header>
            <div class="proxy-page__card-header">
              <span class="proxy-page__card-title">扩展脚本（JavaScript）</span>
              <span class="proxy-page__hint">
                定义 function main(config) 并返回新的 config；沙箱内无文件与网络访问，
                可用 console.log 调试
              </span>
            </div>
          </template>
          <CodeEditor
            v-model="extendScript"
            language="javascript"
            height="260px"
            :fallback-rows="12"
            :loading="extendLoading"
          />
          <div class="proxy-page__extend-actions">
            <el-button :loading="extendPreviewing" @click="handlePreviewExtend">预览</el-button>
            <el-button type="primary" :loading="extendSaving" @click="saveExtend">
              保存并应用
            </el-button>
          </div>
        </el-card>

        <el-card v-if="extendPreview || extendLogs.length" shadow="never" class="proxy-page__card">
          <template #header>
            <span class="proxy-page__card-title">预览结果（未写入磁盘）</span>
          </template>
          <el-alert
            v-if="extendLogs.length"
            type="info"
            :closable="false"
            :title="`脚本输出：${extendLogs.join(' | ')}`"
            class="proxy-page__config-alert"
          />
          <CodeEditor v-model="extendPreview" language="yaml" readonly height="360px" />
        </el-card>
      </el-tab-pane>

      <!-- 配置文件：合成结果，可直接编辑（下次重新生成会覆盖） -->
      <el-tab-pane label="配置文件" name="config">
        <el-card shadow="never" class="proxy-page__card">
          <template #header>
            <div class="proxy-page__card-header">
              <span class="proxy-page__config-path">{{ configPath || 'Clash 配置文件' }}</span>
              <div class="proxy-page__config-actions">
                <template v-if="configEditing">
                  <el-button :loading="configLoading" @click="reloadConfig">放弃修改</el-button>
                  <el-button type="primary" :loading="configSaving" @click="saveConfig">
                    保存并重载
                  </el-button>
                </template>
                <template v-else>
                  <el-button :loading="configLoading" @click="reloadConfig">刷新</el-button>
                  <el-button type="primary" :disabled="!configLoaded" @click="startEditing">
                    编辑
                  </el-button>
                </template>
              </div>
            </div>
          </template>
          <el-alert
            v-if="configEditing"
            type="warning"
            :closable="false"
            show-icon
            title="保存将写入磁盘并触发控制器重载，可能需要再次验证身份；面板自定义规则块会自动保留"
            class="proxy-page__config-alert"
          />
          <CodeEditor
            ref="configEditor"
            v-model="configContent"
            language="yaml"
            height="calc(100vh - 300px)"
            :readonly="!configEditing"
            :loading="configLoading"
          />

        </el-card>
      </el-tab-pane>

      <!-- 连接：活跃连接 + 最近关闭 -->
      <el-tab-pane label="连接" name="connections">
        <el-alert
          v-if="controllerUnavailable"
          type="warning"
          title="控制器当前不可达，实时连接不可用；下方仍可查看历史关闭记录"
          :closable="false"
          show-icon
          class="proxy-page__conn-alert"
        />
        <el-card shadow="never" class="proxy-page__card">
          <template #header>
            <div class="proxy-page__card-header">
              <div class="proxy-page__card-title-group">
                <span class="proxy-page__card-title">活跃连接</span>
                <el-tag size="small" type="info" effect="plain">
                  {{ connectionSummary.count }} 条
                </el-tag>
                <span class="proxy-page__hint">
                  累计 ↓ {{ formatBytes(connectionSummary.download) }} · ↑
                  {{ formatBytes(connectionSummary.upload) }}
                </span>
              </div>
              <div class="proxy-page__config-actions">
                <el-button
                  link
                  type="primary"
                  :loading="connectionsPending"
                  @click="() => refetchConnections()"
                >
                  刷新
                </el-button>
                <el-button
                  type="danger"
                  plain
                  :loading="clearingConnections"
                  @click="handleClearConnections"
                >
                  清空全部连接
                </el-button>
              </div>
            </div>
          </template>
          <el-skeleton v-if="connectionsPending" :rows="4" animated />
          <el-alert
            v-else-if="connectionsIsError"
            type="error"
            :title="connectionsMessage"
            :closable="false"
            show-icon
          />
          <el-table v-else :data="activeConnections" size="small" max-height="420">
            <template #empty>
              <el-empty description="暂无活跃连接" :image-size="60" />
            </template>
            <el-table-column label="主机" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="proxy-page__conn-host">{{ row.host || row.destination || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="网络" width="90">
              <template #default="{ row }">
                <el-tag size="small" effect="plain" type="info">
                  {{ (row.network || '—').toUpperCase() }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="规则" min-width="170" show-overflow-tooltip>
              <template #default="{ row }">{{ ruleText(row) }}</template>
            </el-table-column>
            <el-table-column label="链路" min-width="170" show-overflow-tooltip>
              <template #default="{ row }">{{ chainText(row) }}</template>
            </el-table-column>
            <el-table-column label="速率" width="170">
              <template #default="{ row }">
                <span class="proxy-page__conn-rate">
                  ↓ {{ formatRate(row.download_rate ?? 0) }} ↑ {{ formatRate(row.upload_rate ?? 0) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="下载" width="90">
              <template #default="{ row }">{{ formatBytes(row.download) }}</template>
            </el-table-column>
            <el-table-column label="上传" width="90">
              <template #default="{ row }">{{ formatBytes(row.upload) }}</template>
            </el-table-column>
            <el-table-column label="开始时间" width="160">
              <template #default="{ row }">
                {{ row.start ? formatDateTime(row.start) : '—' }}
              </template>
            </el-table-column>
            <el-table-column label="操作" width="80" fixed="right" align="right">
              <template #default="{ row }">
                <el-button
                  link
                  type="danger"
                  :loading="closingConnId === row.id"
                  @click="handleCloseConnection(row)"
                >
                  关闭
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never" class="proxy-page__card">
          <template #header>
            <div class="proxy-page__card-header">
              <div class="proxy-page__card-title-group">
                <span class="proxy-page__card-title">最近关闭</span>
                <el-tag size="small" type="info" effect="plain">
                  {{ closedConnections.length }} 条
                </el-tag>
                <span class="proxy-page__hint">面板启动后关闭的连接，最多保留 100 条</span>
              </div>
            </div>
          </template>
          <el-table :data="closedConnections" size="small" max-height="420">
            <template #empty>
              <el-empty description="暂无关闭记录" :image-size="60" />
            </template>
            <el-table-column label="主机" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="proxy-page__conn-host">{{ row.host || row.destination || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="网络" width="90">
              <template #default="{ row }">
                <el-tag size="small" effect="plain" type="info">
                  {{ (row.network || '—').toUpperCase() }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="规则" min-width="170" show-overflow-tooltip>
              <template #default="{ row }">{{ ruleText(row) }}</template>
            </el-table-column>
            <el-table-column label="链路" min-width="170" show-overflow-tooltip>
              <template #default="{ row }">{{ chainText(row) }}</template>
            </el-table-column>
            <el-table-column label="下载" width="90">
              <template #default="{ row }">{{ formatBytes(row.download) }}</template>
            </el-table-column>
            <el-table-column label="上传" width="90">
              <template #default="{ row }">{{ formatBytes(row.upload) }}</template>
            </el-table-column>
            <el-table-column label="关闭时间" width="160">
              <template #default="{ row }">
                {{ row.closed_at ? formatDateTime(row.closed_at) : '—' }}
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <ReauthDialog
      v-model="reauthVisible"
      message="保存 Clash 配置需要再次验证身份，请输入登录密码。"
      @success="onReauthSuccess"
    />
  </div>
</template>

<style scoped>
.proxy-page__row {
  margin-bottom: 16px;
}

.proxy-page__card {
  margin-bottom: 16px;
}

.proxy-page__card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.proxy-page__card-title {
  font-weight: 600;
}

.proxy-page__card-title-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.proxy-page__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.proxy-page__service {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 12px;
  margin-bottom: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.proxy-page__traffic-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.proxy-page__rates {
  display: flex;
  align-items: center;
  gap: 16px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.proxy-page__rate {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.proxy-page__mode {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 16px;
}

.proxy-page__label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.proxy-page__group {
  padding: 12px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.proxy-page__group:last-child {
  border-bottom: none;
}

.proxy-page__group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.proxy-page__group-title {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  user-select: none;
}

.proxy-page__group-caret {
  color: var(--el-text-color-secondary);
}

.proxy-page__group-count {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.proxy-page__group-name {
  font-weight: 600;
}

.proxy-page__group-now {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.proxy-page__nodes {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.proxy-page__node {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--el-fill-color-blank);
  font-size: 13px;
  max-width: 100%;
}

.proxy-page__node.is-selectable {
  cursor: pointer;
}

.proxy-page__node.is-selectable:hover {
  border-color: var(--el-color-primary);
}

.proxy-page__node.is-active {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-weight: 600;
}

.proxy-page__node-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 240px;
}

/* 延迟标签本身可点，用于单独复测这一个节点。 */
.proxy-page__node-delay {
  cursor: pointer;
}

.proxy-page__profile-tag {
  margin-left: 8px;
}

.proxy-page__profile-select {
  width: 200px;
}

.proxy-page__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  max-width: 60%;
  text-align: right;
}

.proxy-page__extend-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}

.proxy-page__rule-form {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.proxy-page__rule-type {
  width: 180px;
}

.proxy-page__rule-value {
  flex: 1;
  min-width: 200px;
}

.proxy-page__rule-policy {
  width: 180px;
}

.proxy-page__rule-preview {
  margin-bottom: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.proxy-page__rule-code {
  font-family: var(--el-font-family-mono, monospace);
  font-size: 12px;
}

.proxy-page__rule-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.proxy-page__rule-search {
  width: 220px;
}

.proxy-page__rule-pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.proxy-page__conn-alert {
  margin-bottom: 16px;
}

.proxy-page__conn-host {
  font-weight: 500;
}

.proxy-page__conn-rate {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  white-space: nowrap;
}

.proxy-page__config-path {
  font-weight: 600;
  word-break: break-all;
}

.proxy-page__config-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.proxy-page__config-alert {
  margin-bottom: 8px;
}
</style>
