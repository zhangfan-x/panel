<script setup lang="ts">
// Docker 守护进程配置：/etc/docker/daemon.json 的表单视图 + 原始 JSON 视图。
//
// 单一真源是 rawText（保存时提交的就是它）。表单只是它的一个投影：切到表单页时
// 从 rawText 解析出各字段，改动表单则合并回 rawText —— 合并只覆盖表单认识的键，
// 其它键（用户手写的冷门配置）原样保留。
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import {
  getDaemonConfig,
  isDockerUnavailable,
  listContainers,
  putDaemonConfig,
  restartDaemon,
} from '@/api/docker'
import { problemMessage } from '@/api/problem'
import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'
import ReauthDialog from '@/components/ReauthDialog.vue'
import { formatDateTime } from '@/composables/format'

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['docker-daemon-config'],
  queryFn: getDaemonConfig,
})

const errorMessage = computed(() => problemMessage(error.value, '读取 Docker 配置失败'))
const dockerUnavailable = computed(() => isDockerUnavailable(error.value))

const activeTab = ref<'form' | 'raw'>('form')
const rawText = ref('')
const saving = ref(false)
const reauthVisible = ref(false)
/** true 表示正在由 rawText 回填表单，此时不要再把表单写回 rawText。 */
let syncing = false

const form = reactive({
  httpProxy: '',
  httpsProxy: '',
  noProxy: '',
  registryMirrors: '',
  insecureRegistries: '',
  logDriver: '',
  logMaxSize: '',
  logMaxFile: '',
  liveRestore: false,
})

const LOG_DRIVERS = ['json-file', 'local', 'journald', 'syslog', 'none']

/** 解析当前 rawText；失败时返回 null（原始视图会显示错误）。 */
function parseRaw(): Record<string, unknown> | null {
  const text = rawText.value.trim()
  if (!text) {
    return {}
  }
  try {
    const parsed: unknown = JSON.parse(text)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    return null
  }
}

const rawError = computed(() => (parseRaw() === null ? 'JSON 格式不合法，无法解析' : ''))

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** 字符串数组 → 每行一项的文本域内容。 */
function listToText(value: unknown): string {
  return Array.isArray(value) ? value.filter((v) => typeof v === 'string').join('\n') : ''
}

/** 每行一项的文本域内容 → 去空白后的字符串数组。 */
function textToList(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}

function syncFormFromRaw(): void {
  const obj = parseRaw()
  if (obj === null) {
    return
  }
  syncing = true
  const proxies = (obj['proxies'] ?? {}) as Record<string, unknown>
  form.httpProxy = asString(proxies['http-proxy'])
  form.httpsProxy = asString(proxies['https-proxy'])
  form.noProxy = asString(proxies['no-proxy'])
  form.registryMirrors = listToText(obj['registry-mirrors'])
  form.insecureRegistries = listToText(obj['insecure-registries'])
  form.logDriver = asString(obj['log-driver'])
  const logOpts = (obj['log-opts'] ?? {}) as Record<string, unknown>
  form.logMaxSize = asString(logOpts['max-size'])
  form.logMaxFile = asString(logOpts['max-file'])
  form.liveRestore = obj['live-restore'] === true
  // 回填会触发下面的 watch；等 Vue 冲刷完这一轮再解除标记，避免把刚读出来的
  // 表单值又原样写回 rawText。
  void nextTick(() => {
    syncing = false
  })
}

/** 表单字段合并回 rawText：置空的字段会从 JSON 中删除，而不是留下空串。 */
function applyFormToRaw(): void {
  const obj = parseRaw()
  if (obj === null) {
    return // 原始 JSON 当前不合法，不要用表单覆盖掉用户正在修的内容
  }

  const proxies: Record<string, unknown> = {
    ...((obj['proxies'] as Record<string, unknown> | undefined) ?? {}),
  }
  setOrDelete(proxies, 'http-proxy', form.httpProxy.trim())
  setOrDelete(proxies, 'https-proxy', form.httpsProxy.trim())
  setOrDelete(proxies, 'no-proxy', form.noProxy.trim())
  if (Object.keys(proxies).length > 0) {
    obj['proxies'] = proxies
  } else {
    delete obj['proxies']
  }

  setListOrDelete(obj, 'registry-mirrors', textToList(form.registryMirrors))
  setListOrDelete(obj, 'insecure-registries', textToList(form.insecureRegistries))
  setOrDelete(obj, 'log-driver', form.logDriver.trim())

  const logOpts: Record<string, unknown> = {
    ...((obj['log-opts'] as Record<string, unknown> | undefined) ?? {}),
  }
  setOrDelete(logOpts, 'max-size', form.logMaxSize.trim())
  setOrDelete(logOpts, 'max-file', form.logMaxFile.trim())
  if (Object.keys(logOpts).length > 0) {
    obj['log-opts'] = logOpts
  } else {
    delete obj['log-opts']
  }

  if (form.liveRestore) {
    obj['live-restore'] = true
  } else {
    delete obj['live-restore']
  }

  rawText.value = JSON.stringify(obj, null, 2)
}

function setOrDelete(target: Record<string, unknown>, key: string, value: string): void {
  if (value) {
    target[key] = value
  } else {
    delete target[key]
  }
}

function setListOrDelete(target: Record<string, unknown>, key: string, value: string[]): void {
  if (value.length > 0) {
    target[key] = value
  } else {
    delete target[key]
  }
}

watch(
  () => data.value,
  (config) => {
    if (!config) {
      return
    }
    rawText.value = config.content.trim() ? config.content : '{}'
    syncFormFromRaw()
  },
  { immediate: true },
)

watch(form, () => {
  if (!syncing) {
    applyFormToRaw()
  }
})

watch(activeTab, (tab) => {
  if (tab === 'form') {
    syncFormFromRaw()
  }
})

const restartPending = computed(() => data.value?.restart_pending ?? false)
const configPath = computed(() => data.value?.path || '/etc/docker/daemon.json')
const mtimeText = computed(() => {
  const mtime = data.value?.mtime ?? 0
  return mtime ? formatDateTime(new Date(mtime * 1000).toISOString()) : '—'
})

// —— 保存 ——

async function save(): Promise<void> {
  if (rawError.value) {
    ElMessage.error(rawError.value)
    return
  }
  saving.value = true
  try {
    const result = await putDaemonConfig(rawText.value)
    rawText.value = result.content
    ElMessage.success(
      result.backup ? `已保存，原配置备份为 ${result.backup}` : '已保存（此前没有配置文件）',
    )
    await refetch()
  } catch (err) {
    if ((err as ApiError | undefined)?.problem?.code === 'auth.reauth_required') {
      reauthVisible.value = true
    } else {
      ElMessage.error(problemMessage(err, '保存失败'))
    }
  } finally {
    saving.value = false
  }
}

function onReauthSuccess(): void {
  void save()
}

// —— 重启守护进程 ——

const restartVisible = ref(false)
const restarting = ref(false)
const runningCount = ref(0)

async function openRestart(): Promise<void> {
  runningCount.value = 0
  try {
    const list = await listContainers(false)
    runningCount.value = list.containers.length
  } catch {
    // 拿不到容器数不影响重启，确认框会退化为不带数量的提示。
  }
  restartVisible.value = true
}

const restartWarning = computed(() => {
  const live = form.liveRestore ? '当前已开启 live-restore，运行中的容器不会被中断。' : ''
  if (live) {
    return `重启 Docker 守护进程以应用新配置。${live}`
  }
  const count = runningCount.value
  const affected =
    count > 0
      ? `这会重启当前 ${count} 个运行中的容器`
      : '这会重启所有运行中的容器（当前没有正在运行的容器）'
  return `重启 Docker 守护进程以应用新配置。未开启 live-restore，${affected}。`
})

async function handleRestart(): Promise<void> {
  restarting.value = true
  try {
    await restartDaemon()
    ElMessage.success('Docker 守护进程已重启')
    restartVisible.value = false
    await refetch()
  } catch (err) {
    if ((err as ApiError | undefined)?.problem?.code === 'auth.reauth_required') {
      restartVisible.value = false
      reauthVisible.value = true
    } else {
      ElMessage.error(problemMessage(err, '重启失败'))
    }
  } finally {
    restarting.value = false
  }
}
</script>

<template>
  <div class="daemon-config">
    <el-card shadow="never">
      <template #header>
        <div class="daemon-config__header">
          <div>
            <span class="daemon-config__title">Docker 守护进程配置</span>
            <span class="daemon-config__path">{{ configPath }}</span>
          </div>
          <div class="daemon-config__actions">
            <el-button :loading="saving" type="primary" @click="save">保存</el-button>
            <el-button :loading="restarting" @click="openRestart">重启 Docker</el-button>
          </div>
        </div>
      </template>

      <el-alert
        v-if="isError"
        :title="dockerUnavailable ? 'Docker 守护进程不可用' : errorMessage"
        type="error"
        :closable="false"
        show-icon
      />
      <el-skeleton v-else-if="isPending" :rows="6" animated />
      <template v-else>
        <el-alert
          v-if="data && !data.exists"
          title="尚未创建 daemon.json，保存后会新建该文件"
          type="info"
          :closable="false"
          show-icon
          class="daemon-config__notice"
        />
        <el-alert
          v-if="data?.parse_error"
          :title="`现有 daemon.json 无法解析（${data.parse_error}），表单已留空，请在「原始 JSON」中修复`"
          type="warning"
          :closable="false"
          show-icon
          class="daemon-config__notice"
        />
        <el-alert
          v-if="restartPending"
          title="配置文件比运行中的守护进程新，改动尚未生效，需要重启 Docker"
          type="warning"
          :closable="false"
          show-icon
          class="daemon-config__notice"
        />

        <div class="daemon-config__meta">最后修改：{{ mtimeText }}</div>

        <el-tabs v-model="activeTab">
          <el-tab-pane label="常用设置" name="form">
            <el-form label-width="140px" class="daemon-config__form">
              <el-divider content-position="left">代理</el-divider>
              <el-form-item label="HTTP 代理">
                <el-input v-model="form.httpProxy" placeholder="http://127.0.0.1:7890" />
              </el-form-item>
              <el-form-item label="HTTPS 代理">
                <el-input v-model="form.httpsProxy" placeholder="http://127.0.0.1:7890" />
              </el-form-item>
              <el-form-item label="不走代理">
                <el-input v-model="form.noProxy" placeholder="localhost,127.0.0.1,::1" />
                <div class="daemon-config__hint">
                  逗号分隔。代理只影响守护进程自身拉取镜像，不影响容器内的网络。
                </div>
              </el-form-item>

              <el-divider content-position="left">镜像仓库</el-divider>
              <el-form-item label="镜像加速地址">
                <el-input
                  v-model="form.registryMirrors"
                  type="textarea"
                  :rows="3"
                  placeholder="https://registry.example.com"
                />
                <div class="daemon-config__hint">每行一个地址。</div>
              </el-form-item>
              <el-form-item label="不安全仓库">
                <el-input
                  v-model="form.insecureRegistries"
                  type="textarea"
                  :rows="2"
                  placeholder="registry.internal:5000"
                />
                <div class="daemon-config__hint">
                  每行一个。这些仓库将允许 HTTP 或自签名证书，请谨慎使用。
                </div>
              </el-form-item>

              <el-divider content-position="left">日志</el-divider>
              <el-form-item label="日志驱动">
                <el-select v-model="form.logDriver" clearable placeholder="使用 Docker 默认">
                  <el-option v-for="d in LOG_DRIVERS" :key="d" :label="d" :value="d" />
                </el-select>
              </el-form-item>
              <el-form-item label="单文件上限">
                <el-input v-model="form.logMaxSize" placeholder="10m" />
              </el-form-item>
              <el-form-item label="保留文件数">
                <el-input v-model="form.logMaxFile" placeholder="3" />
                <div class="daemon-config__hint">
                  仅对新建的容器生效，已存在的容器需重建才会应用。
                </div>
              </el-form-item>

              <el-divider content-position="left">其它</el-divider>
              <el-form-item label="live-restore">
                <el-switch v-model="form.liveRestore" />
                <div class="daemon-config__hint">
                  开启后重启守护进程不会中断运行中的容器；与 swarm 模式不兼容。
                </div>
              </el-form-item>
            </el-form>
          </el-tab-pane>

          <el-tab-pane label="原始 JSON" name="raw">
            <el-alert
              v-if="rawError"
              :title="rawError"
              type="error"
              :closable="false"
              show-icon
              class="daemon-config__notice"
            />
            <el-input
              v-model="rawText"
              type="textarea"
              :rows="20"
              spellcheck="false"
              class="daemon-config__raw"
            />
            <div class="daemon-config__hint">
              表单未覆盖的字段可以在这里直接编辑，保存时会原样保留。
            </div>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-card>

    <ConfirmDangerDialog
      v-model="restartVisible"
      title="重启 Docker 守护进程"
      resource-name="docker"
      :message="restartWarning"
      :loading="restarting"
      @confirm="handleRestart"
    />

    <ReauthDialog
      v-model="reauthVisible"
      message="修改 Docker 守护进程配置需要再次验证身份，请输入登录密码。"
      @success="onReauthSuccess"
    />
  </div>
</template>

<style scoped>
.daemon-config__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.daemon-config__title {
  font-weight: 600;
}

.daemon-config__path {
  margin-left: 10px;
  color: var(--el-text-color-secondary);
  font-family: var(--el-font-family-monospace, monospace);
  font-size: 12px;
}

.daemon-config__actions {
  display: flex;
  gap: 8px;
}

.daemon-config__notice {
  margin-bottom: 12px;
}

.daemon-config__meta {
  margin-bottom: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.daemon-config__form {
  max-width: 720px;
}

.daemon-config__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.daemon-config__raw :deep(textarea) {
  font-family: var(--el-font-family-monospace, monospace);
}
</style>
