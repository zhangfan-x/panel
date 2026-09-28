<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import type { FitAddon } from '@xterm/addon-fit'
import type { Terminal } from '@xterm/xterm'

import { containerExecWsUrl } from '@/api/docker'

/**
 * 容器 exec 终端：在浏览器 xterm 与后端 `WS /docker/containers/{id}/exec` 之间桥接。
 * 后端经 root helper 的 docker.exec_create 打开容器内 `/bin/sh`（TTY）劫持 socket。
 * 协议：二进制帧 = exec I/O；JSON 文本控制帧 {"type":"resize",cols,rows}；
 * 服务端失败时下发 {"type":"error",code,message} 再关闭连接。
 * 认证走同源会话 cookie（无票据）；xterm 及 fit 插件按需懒加载（独立 'xterm' chunk）。
 */

const props = defineProps<{
  /** 目标容器 id。 */
  containerId: string
  /** 容器名（仅用于展示）。 */
  containerName?: string
}>()

const emit = defineEmits<{
  /** 会话结束（Shell 退出、连接关闭或出错），供父级关闭抽屉。 */
  (e: 'close'): void
}>()

type ExecStatus = 'connecting' | 'connected' | 'closed' | 'error'

/** 服务端 JSON 控制帧（error：exec 打开失败，如容器不存在 / 守护进程不可用）。 */
interface ControlFrame {
  type?: string
  code?: string
  message?: string
}

const status = ref<ExecStatus>('connecting')
const errorMessage = ref('')
const containerRef = ref<HTMLDivElement | null>(null)

// 非响应式连接期资源（随组件卸载销毁）。
let ws: WebSocket | null = null
let term: Terminal | null = null
let fitAddon: FitAddon | null = null
let resizeObserver: ResizeObserver | null = null
const encoder = new TextEncoder()

const statusText = computed(() => {
  if (status.value === 'connected') {
    return '已连接'
  }
  if (status.value === 'connecting') {
    return '连接中'
  }
  return '已断开'
})

/** 若 WebSocket 已打开，发送二进制 exec 输入帧。 */
function send(data: Uint8Array): void {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(data)
  }
}

/** 若 WebSocket 已打开，发送 JSON 控制帧（如 resize）。 */
function sendControl(frame: Record<string, unknown>): void {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(frame))
  }
}

/** 懒加载 xterm（打包为独立 'xterm' chunk）并创建终端实例。 */
async function createTerm(): Promise<void> {
  destroyTerm()
  const [{ Terminal: XTerminal }, { FitAddon: XFitAddon }] = await Promise.all([
    import('@xterm/xterm'),
    import('@xterm/addon-fit'),
    import('@xterm/xterm/css/xterm.css'),
  ])
  const instance = new XTerminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: "Menlo, Consolas, 'DejaVu Sans Mono', 'Courier New', monospace",
    scrollback: 5000,
    theme: {
      background: '#1d1e1f',
      foreground: '#e5eaf3',
    },
  })
  const fit = new XFitAddon()
  instance.loadAddon(fit)
  instance.onData((data) => {
    send(encoder.encode(data))
  })
  instance.onBinary((data) => {
    // onBinary 的字符串每个 charCode 即一个原始字节（二进制粘贴场景）。
    send(Uint8Array.from(data, (ch) => ch.charCodeAt(0) & 0xff))
  })
  instance.onResize(({ cols, rows }) => {
    sendControl({ type: 'resize', cols, rows })
  })
  term = instance
  fitAddon = fit
}

function openSocket(): void {
  const socket = new WebSocket(containerExecWsUrl(props.containerId))
  socket.binaryType = 'arraybuffer'
  ws = socket
  socket.onopen = () => {
    if (ws !== socket) {
      return
    }
    void attachTerm()
  }
  socket.onmessage = (event: MessageEvent<ArrayBuffer | string>) => {
    if (ws !== socket) {
      return
    }
    if (typeof event.data === 'string') {
      handleControl(event.data)
    } else {
      term?.write(new Uint8Array(event.data))
    }
  }
  socket.onclose = (event: CloseEvent) => {
    if (ws !== socket) {
      return
    }
    ws = null
    finishSession(event.code)
  }
  socket.onerror = () => {
    // 出错后浏览器随后必然触发 onclose，这里无需额外处理。
  }
}

/** WebSocket 打开后把 xterm 挂载到容器并自适应尺寸。 */
async function attachTerm(): Promise<void> {
  status.value = 'connected'
  await nextTick()
  const container = containerRef.value
  const instance = term
  if (!container || !instance) {
    return
  }
  instance.open(container)
  fitNow()
  instance.focus()
  resizeObserver = new ResizeObserver(() => {
    fitNow()
  })
  resizeObserver.observe(container)
}

function fitNow(): void {
  try {
    fitAddon?.fit()
  } catch {
    // 容器尚不可见时 fit 可能失败，忽略即可（下次尺寸变化会重试）。
  }
}

function handleControl(text: string): void {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return
  }
  if (!parsed || typeof parsed !== 'object') {
    return
  }
  const frame = parsed as ControlFrame
  if (frame.type === 'error') {
    // 服务端在打开 exec 失败（容器不存在 / 守护进程不可用）时下发 error 帧后关闭。
    status.value = 'error'
    errorMessage.value = frame.message ?? '无法进入容器终端'
  }
}

/** 连接结束的统一收尾：释放终端资源并落到对应状态。 */
function finishSession(closeCode: number): void {
  resizeObserver?.disconnect()
  resizeObserver = null
  destroyTerm()
  if (status.value === 'error') {
    return
  }
  if (closeCode === 1008) {
    status.value = 'error'
    errorMessage.value = '连接被拒绝，请确认已登录并具有权限'
    return
  }
  status.value = 'closed'
}

function destroyTerm(): void {
  term?.dispose()
  term = null
  fitAddon = null
}

/** 主动断开：关闭 WebSocket，服务端随之关闭 exec 并记录审计。 */
function disconnect(): void {
  ws?.close(1000)
}

async function start(): Promise<void> {
  status.value = 'connecting'
  errorMessage.value = ''
  try {
    await createTerm()
  } catch {
    status.value = 'error'
    errorMessage.value = '终端组件加载失败，请刷新页面后重试'
    return
  }
  openSocket()
}

onMounted(() => {
  void start()
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  if (ws) {
    const socket = ws
    ws = null
    socket.onopen = null
    socket.onmessage = null
    socket.onclose = null
    socket.onerror = null
    socket.close(1000)
  }
  destroyTerm()
})
</script>

<template>
  <div class="container-terminal">
    <div class="container-terminal__bar">
      <div class="container-terminal__meta">
        <el-tag v-if="status === 'connected'" type="success" effect="plain" size="small">
          {{ statusText }}
        </el-tag>
        <el-tag v-else type="info" effect="plain" size="small">{{ statusText }}</el-tag>
        <span class="container-terminal__hint">/bin/sh · {{ containerName ?? containerId }}</span>
      </div>
      <div class="container-terminal__actions">
        <el-button
          v-if="status === 'connected' || status === 'connecting'"
          type="danger"
          plain
          size="small"
          @click="disconnect"
        >
          断开
        </el-button>
        <el-button v-else size="small" @click="start">重新连接</el-button>
        <el-button size="small" @click="emit('close')">关闭</el-button>
      </div>
    </div>

    <el-alert
      v-if="status === 'error'"
      type="error"
      :title="errorMessage || '容器终端不可用'"
      :closable="false"
      show-icon
      class="container-terminal__alert"
    />

    <div
      v-loading="status === 'connecting'"
      element-loading-text="正在进入容器终端…"
      class="container-terminal__wrap"
    >
      <div ref="containerRef" class="container-terminal__term" />
    </div>
  </div>
</template>

<style scoped>
.container-terminal {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 8px;
}

.container-terminal__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.container-terminal__meta {
  display: flex;
  align-items: center;
  gap: 10px;
}

.container-terminal__hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.container-terminal__actions {
  display: flex;
  gap: 8px;
}

.container-terminal__alert {
  flex: none;
}

.container-terminal__wrap {
  flex: 1 1 auto;
  min-height: 320px;
  padding: 8px;
  border-radius: 4px;
  background: #1d1e1f;
}

.container-terminal__term {
  height: 100%;
}
</style>
